"""Receipt schema versions, gate logs, verify_run for old receipts, and non-data ledger events."""

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from gdkit.audit import verify_run
from gdkit.cards import CARD_SCHEMA, audit_card, dataset_card, write_card
from gdkit.gates import RECEIPT_SCHEMA, Gate, receipt_hash, run_gate
from gdkit.ledger import Ledger
from gdkit.promote import PromoteError, attestable_hash, git_attestation, promote
from prereg_fixture import freeze, proj

RUN_ID = "run-1"
ACCEPT = lambda run_id, gate, digest: True  # noqa: E731


def noisy_check(run_dir):
    print("checking rows")
    return {"passed": True, "rows": 3}


def quiet_expensive(run_dir):
    print("training")
    return {"passed": True, "loss": 0.5}


NOISY = Gate("noisy", cheap=True, check=noisy_check)
TRAIN = Gate("train", cheap=False, check=quiet_expensive)
REGISTRY = {"noisy": NOISY, "train": TRAIN}


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _head(run):
    return json.loads((run / "ledger.jsonl").read_text(encoding="utf-8").splitlines()[-1])["entry_hash"]


def make_run(tmp_path, *, capture_log=False):
    run = tmp_path / "run"
    run.mkdir()
    data = run / "data.csv"
    data.write_text("a\nb\nc\n", encoding="utf-8")
    ledger = Ledger(run / "ledger.jsonl", run_id=RUN_ID, mode="registered")
    freeze(ledger, run)
    ledger.record("load", rows_before=0, rows_after=3, input_hash=_sha(data), output_hash=_sha(data))
    for gate in (NOISY, TRAIN):
        run_gate(run, gate, inputs=["data.csv"], run_id=RUN_ID, ledger_head=_head(run), capture_log=capture_log)
    return run


def receipt_path(run, name):
    return run / "receipts" / f"{name}.json"


def rewrite(run, name, mutate):
    body = json.loads(receipt_path(run, name).read_text(encoding="utf-8"))
    mutate(body)
    body["receipt_hash"] = receipt_hash(body)
    receipt_path(run, name).write_text(json.dumps(body, sort_keys=True), encoding="utf-8")


def as_schema_0(body, result=None):
    """What an 8cf73ec receipt looks like: the same keys, no receipt_schema, no log, an 'ok' result."""
    for key in ("receipt_schema", "log", "contract_version", "census"):
        body.pop(key, None)
    body["result"] = result if result is not None else {"ok": True, "problems": []}


# --- the schema field ------------------------------------------------------------------------


def test_run_gate_writes_the_current_receipt_schema(tmp_path):
    run = make_run(tmp_path)
    assert json.loads(receipt_path(run, "noisy").read_text(encoding="utf-8"))["receipt_schema"] == RECEIPT_SCHEMA == 1


def test_a_schema_0_receipt_is_refused_by_promote_and_attest(tmp_path):
    run = make_run(tmp_path)
    rewrite(run, "noisy", as_schema_0)
    with pytest.raises(PromoteError, match="noisy: .*schema 0.*re-run the gate"):
        promote(run, REGISTRY, ["noisy"], attestation=ACCEPT, project_path=proj(run))
    rewrite(run, "train", as_schema_0)
    with pytest.raises(PromoteError, match="train: .*schema 0"):
        attestable_hash(run, "train", REGISTRY, project_path=proj(run))


def test_a_failing_receipt_cannot_dodge_the_pass_rule_by_dropping_its_version(tmp_path):
    run = make_run(tmp_path)
    rewrite(run, "noisy", lambda body: as_schema_0(body, {"passed": False}))
    with pytest.raises(PromoteError, match="schema 0"):
        promote(run, REGISTRY, ["noisy"], attestation=ACCEPT, project_path=proj(run))


def test_an_unknown_schema_is_refused_as_newer(tmp_path):
    run = make_run(tmp_path)
    rewrite(run, "noisy", lambda body: body.update(receipt_schema=99))
    with pytest.raises(PromoteError, match="newer gdkit"):
        promote(run, REGISTRY, ["noisy"], attestation=ACCEPT, project_path=proj(run))


@pytest.mark.parametrize("value", ["1", True, 1.0, None])
def test_a_schema_that_is_not_an_integer_is_refused(tmp_path, value):
    run = make_run(tmp_path)
    rewrite(run, "noisy", lambda body: body.update(receipt_schema=value))
    with pytest.raises(PromoteError, match="not an integer"):
        promote(run, REGISTRY, ["noisy"], attestation=ACCEPT, project_path=proj(run))


def test_the_hash_check_comes_before_the_schema_check(tmp_path):
    run = make_run(tmp_path)
    body = json.loads(receipt_path(run, "noisy").read_text(encoding="utf-8"))
    body["receipt_schema"] = 99  # hash not recomputed
    receipt_path(run, "noisy").write_text(json.dumps(body), encoding="utf-8")
    with pytest.raises(PromoteError, match="does not match its body"):
        promote(run, REGISTRY, ["noisy"], attestation=ACCEPT, project_path=proj(run))


# --- gate logs -------------------------------------------------------------------------------


def test_a_captured_log_is_written_by_gdkit_and_hashed_into_the_receipt(tmp_path):
    run = make_run(tmp_path, capture_log=True)
    receipt = json.loads(receipt_path(run, "noisy").read_text(encoding="utf-8"))
    log = run / receipt["log"]["path"]
    assert receipt["log"]["path"] == "logs/noisy.log"
    assert log.read_bytes() == b"checking rows\n"
    assert receipt["log"]["sha256"] == _sha(log) and receipt["log"]["bytes"] == 14


def test_promote_twice_in_a_row_does_not_rewrite_the_log(tmp_path):
    run = make_run(tmp_path, capture_log=True)
    before = (run / "logs" / "noisy.log").read_bytes()
    for _ in range(2):
        promote(run, REGISTRY, ["noisy", "train"], attestation=ACCEPT, project_path=proj(run))
    assert (run / "logs" / "noisy.log").read_bytes() == before


def test_a_changed_log_is_refused(tmp_path):
    run = make_run(tmp_path, capture_log=True)
    (run / "logs" / "noisy.log").write_bytes(b"something else\n")
    with pytest.raises(PromoteError, match="noisy: log logs/noisy.log has changed"):
        promote(run, REGISTRY, ["noisy"], attestation=ACCEPT, project_path=proj(run))


def test_an_absent_log_is_reported_not_refused(tmp_path):
    run = make_run(tmp_path, capture_log=True)
    shutil.rmtree(run / "logs")  # git-ignored logs are absent in a CI checkout
    report = promote(run, REGISTRY, ["noisy", "train"], attestation=ACCEPT, project_path=proj(run))
    assert report["gates"]["noisy"] == "re-run and matched; log absent"
    assert report["gates"]["train"] == "attested; log absent"
    assert attestable_hash(run, "train", REGISTRY, project_path=proj(run))


def test_contract_version_and_census_are_stored_and_hashed(tmp_path):
    from gdkit.census import Census

    run = make_run(tmp_path)
    census = Census("noisy")
    census.discover("data.csv")
    census.passed_item("data.csv")
    path = run_gate(run, NOISY, inputs=[], run_id=RUN_ID, ledger_head=_head(run),
                    contract_version="split-v2", census=census)
    body = json.loads(path.read_text(encoding="utf-8"))
    assert body["contract_version"] == "split-v2"
    assert (body["census"]["discovered"], body["census"]["passed"]) == (1, 1)
    assert body["receipt_hash"] == receipt_hash(body)


def test_a_free_form_census_is_refused(tmp_path):
    from gdkit.gates import GateError

    run = make_run(tmp_path)
    with pytest.raises(GateError, match="census discovered must be a non-negative integer"):
        run_gate(run, NOISY, inputs=[], run_id=RUN_ID, ledger_head=_head(run), census={"rows": 3})


# --- verify_run: read-only report, any schema ------------------------------------------------


def _snapshot(folder):
    return {p.relative_to(folder).as_posix(): _sha(p) for p in sorted(folder.rglob("*")) if p.is_file()}


def test_verify_run_reports_a_schema_0_receipt_without_writing(tmp_path):
    run = make_run(tmp_path)
    rewrite(run, "noisy", as_schema_0)
    before = _snapshot(run)
    report = verify_run(run, registry=REGISTRY)
    assert _snapshot(run) == before
    noisy = report["receipts"]["noisy"]
    assert noisy["schema"] == 0 and noisy["passed"] is None and noisy["promotable"] is False
    assert noisy["ledger_head_seq"] == 1 and noisy["current"] is True
    assert noisy["inputs"] == {"data.csv": "match"} and noisy["code"] == "match"
    assert report["problems"] == []


def test_verify_run_reports_stale_absent_and_changed(tmp_path):
    run = make_run(tmp_path)
    Ledger(run / "ledger.jsonl", run_id=RUN_ID, mode="registered").record(
        "late", rows_before=3, rows_after=3, input_hash="c" * 64, output_hash="c" * 64)
    report = verify_run(run)
    assert report["receipts"]["noisy"]["current"] is False and report["receipts"]["noisy"]["ledger_head_seq"] == 1
    assert report["problems"] == []  # stale is a fact about the run, not a broken receipt
    (run / "data.csv").unlink()
    assert verify_run(run)["receipts"]["noisy"]["inputs"] == {"data.csv": "absent"}
    (run / "data.csv").write_text("changed\n", encoding="utf-8")
    report = verify_run(run)
    assert report["receipts"]["noisy"]["inputs"] == {"data.csv": "changed"}
    assert any("noisy: input data.csv changed" in p for p in report["problems"])


def test_verify_run_flags_a_head_outside_the_chain_and_a_bad_hash(tmp_path):
    run = make_run(tmp_path)
    rewrite(run, "noisy", lambda body: body.update(ledger_head="f" * 64))
    body = json.loads(receipt_path(run, "train").read_text(encoding="utf-8"))
    body["result"] = {"passed": True, "loss": 0.0}
    receipt_path(run, "train").write_text(json.dumps(body), encoding="utf-8")
    problems = verify_run(run)["problems"]
    assert any("noisy: ledger_head is not an entry of this ledger" in p for p in problems)
    assert any("train: receipt hash does not match its body" in p for p in problems)


@pytest.mark.parametrize("content", ["{not json", "[1, 2]"])
def test_verify_run_reports_a_damaged_receipt_instead_of_crashing(tmp_path, content):
    run = make_run(tmp_path)
    receipt_path(run, "noisy").write_text(content, encoding="utf-8")
    report = verify_run(run)
    assert report["receipts"]["noisy"] == {"readable": False}
    assert any(p.startswith("noisy: receipt is not") for p in report["problems"])
    assert report["receipts"]["train"]["hash_ok"] is True


# --- legacy attestation tags (verify only) ---------------------------------------------------

needs_ssh = pytest.mark.skipif(
    shutil.which("ssh-keygen") is None or shutil.which("git") is None,
    reason="git and ssh-keygen are needed for SSH attestation tests",
)


def _git(repo, *args, key=None):
    cmd = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
    if key is not None:
        cmd += ["-c", "gpg.format=ssh", "-c", f"user.signingkey={key}"]
    subprocess.run(cmd + list(args), check=True, capture_output=True, text=True)


@needs_ssh
def test_a_legacy_tag_verifies_under_schema_0_only(tmp_path):
    repo = tmp_path / "repo"
    (repo / "runs" / RUN_ID).mkdir(parents=True)
    _git(repo, "init", "-q")
    (repo / "runs" / RUN_ID / "ledger.jsonl").write_text("{}\n", encoding="utf-8")
    _git(repo, "add", "runs")
    _git(repo, "commit", "-q", "-m", "run")
    key = tmp_path / "owner"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "owner", "-f", str(key)], check=True)
    key_type, key_b64 = Path(str(key) + ".pub").read_text(encoding="utf-8").split()[:2]
    signers = tmp_path / "allowed_signers"
    signers.write_text(f"owner@gdkit {key_type} {key_b64}\n", encoding="utf-8")
    digest = "d" * 64
    _git(repo, "tag", "-s", f"attest/{RUN_ID}", "-m", f"approve {digest}", key=key)
    assert git_attestation(repo, signers, schema=0)(RUN_ID, "train", digest) is True
    assert git_attestation(repo, signers)(RUN_ID, "train", digest) is False


# --- non-data ledger events and card schema --------------------------------------------------


def test_events_are_left_out_of_dataset_counts(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    h = "a" * 64
    ledger = Ledger(run / "ledger.jsonl", run_id="r", mode="registered")
    ledger.record_event("host", params={"summary": {}}, input_hash=h, output_hash=h)
    ledger.record("load", rows_before=0, rows_after=1000, input_hash=h, output_hash=h)
    ledger.record("validate", rows_before=1000, rows_after=950, input_hash=h, output_hash=h)
    ledger.record_event("decision", params={"path": "decisions/000003.json"}, input_hash=h, output_hash=h)
    card = dataset_card(run)
    assert (card["raw_rows"], card["valid_rows"], card["final_rows"]) == (1000, 950, 950)


def test_record_event_in_explore_mode_keeps_hashes_in_params(tmp_path):
    ledger = Ledger(tmp_path / "ledger.jsonl", run_id="r", mode="explore")
    entry = ledger.record_event("host", params={"sha256": "a" * 64})
    assert entry["event"] is True and entry["input_hash"] is None and entry["rows_after"] == 0


def test_an_old_card_is_reported_as_an_old_schema_not_as_field_mismatches(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    h = "a" * 64
    Ledger(run / "ledger.jsonl", run_id="r", mode="registered").record(
        "load", rows_before=0, rows_after=5, input_hash=h, output_hash=h)
    card = dataset_card(run)
    assert card["card_schema"] == CARD_SCHEMA
    old = {k: v for k, v in card.items() if k != "card_schema"}
    path = write_card(tmp_path / "card.json", old)
    assert audit_card(path, card) == [f"card_schema: the card on disk is schema None, current is {CARD_SCHEMA}; regenerate it"]
