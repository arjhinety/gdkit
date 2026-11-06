import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from gdkit.gates import Gate, receipt_hash, run_gate
from prereg_fixture import freeze, proj
from gdkit.ledger import Ledger
from gdkit.promote import PromoteError, attestable_hash, git_attestation, promote

RUN_ID = "run-1"


from gates_fixture import COUNT, REGISTRY, TRAIN  # noqa: E402

REQUIRED = ["row_count", "train"]
ACCEPT = lambda run_id, digest: True  # noqa: E731
REJECT = lambda run_id, digest: False  # noqa: E731


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def make_run(tmp_path, mode="registered"):
    """A run with a verifying ledger and receipts for both gates."""
    run = tmp_path / "run"
    run.mkdir()
    data = run / "data.csv"
    data.write_text("a\nb\nc\n", encoding="utf-8")
    ledger_path = run / "ledger.jsonl"
    ledger = Ledger(ledger_path, run_id=RUN_ID, mode=mode)
    if mode == "registered":
        freeze(ledger, run)
        ledger.record("load", rows_before=0, rows_after=3, input_hash=_sha(data), output_hash=_sha(data))
    else:
        ledger.record("load", rows_before=0, rows_after=3, boundary=True, input_hash=_sha(data), output_hash=_sha(data))
    head = json.loads(ledger_path.read_text(encoding="utf-8").splitlines()[-1])["entry_hash"]
    run_gate(run, COUNT, inputs=["data.csv"], run_id=RUN_ID, ledger_head=head)
    run_gate(run, TRAIN, inputs=["data.csv"], run_id=RUN_ID, ledger_head=head)
    return run


def receipt_path(run, name):
    return run / "receipts" / f"{name}.json"


def load_receipt(run, name):
    return json.loads(receipt_path(run, name).read_text(encoding="utf-8"))


def rewrite_receipt(run, name, mutate):
    """Edit a receipt and recompute its hash, as a forger with file access would."""
    body = load_receipt(run, name)
    mutate(body)
    body["receipt_hash"] = receipt_hash(body)
    receipt_path(run, name).write_text(json.dumps(body, sort_keys=True), encoding="utf-8")


def test_happy_path_is_accepted(tmp_path):
    run = make_run(tmp_path)
    report = promote(run, REGISTRY, REQUIRED, attestation=ACCEPT, project_path=proj(run))
    assert report["gates"] == {"row_count": "re-run and matched", "train": "attested"}


def test_hand_written_receipt_is_refused(tmp_path):
    run = make_run(tmp_path)
    receipt_path(run, "row_count").write_text(
        json.dumps({"run_id": RUN_ID, "gate": "row_count", "result": {"rows": 3}, "receipt_hash": "0" * 64}),
        encoding="utf-8",
    )
    with pytest.raises(PromoteError, match="does not match its body"):
        promote(run, REGISTRY, REQUIRED, attestation=ACCEPT, project_path=proj(run))


def test_forged_result_with_recomputed_hash_fails_the_rerun(tmp_path):
    run = make_run(tmp_path)
    rewrite_receipt(run, "row_count", lambda body: body.update(result={"rows": 99}))
    with pytest.raises(PromoteError, match="does not reproduce"):
        promote(run, REGISTRY, REQUIRED, attestation=ACCEPT, project_path=proj(run))


def test_receipt_copied_from_another_run_is_refused(tmp_path):
    run = make_run(tmp_path)
    rewrite_receipt(run, "row_count", lambda body: body.update(run_id="run-2"))
    with pytest.raises(PromoteError, match="another run"):
        promote(run, REGISTRY, REQUIRED, attestation=ACCEPT, project_path=proj(run))


def test_altered_artifact_is_refused(tmp_path):
    run = make_run(tmp_path)
    (run / "data.csv").write_text("a\nb\nc\nd\n", encoding="utf-8")
    with pytest.raises(PromoteError, match="changed since the receipt"):
        promote(run, REGISTRY, REQUIRED, attestation=ACCEPT, project_path=proj(run))


def test_explore_run_is_refused(tmp_path):
    run = make_run(tmp_path, mode="explore")
    with pytest.raises(PromoteError, match="explore entries"):
        promote(run, REGISTRY, REQUIRED, attestation=ACCEPT, project_path=proj(run))


def test_stale_receipt_after_later_ledger_entry_is_refused(tmp_path):
    run = make_run(tmp_path)
    Ledger(run / "ledger.jsonl", run_id=RUN_ID, mode="registered").record(
        "late_step", rows_before=3, rows_after=3, input_hash="c" * 64, output_hash="c" * 64
    )
    with pytest.raises(PromoteError, match="stale"):
        promote(run, REGISTRY, REQUIRED, attestation=ACCEPT, project_path=proj(run))


def test_receipt_from_an_older_gate_version_is_refused(tmp_path):
    run = make_run(tmp_path)
    rewrite_receipt(run, "row_count", lambda body: body.update(gate_code_hash="f" * 64))
    with pytest.raises(PromoteError, match="different version of the gate code"):
        promote(run, REGISTRY, REQUIRED, attestation=ACCEPT, project_path=proj(run))


def test_expensive_gate_without_attestation_is_refused(tmp_path):
    run = make_run(tmp_path)
    with pytest.raises(PromoteError, match="no valid attestation"):
        promote(run, REGISTRY, REQUIRED, attestation=None, project_path=proj(run))


def test_expensive_gate_rejected_by_attestation_is_refused(tmp_path):
    run = make_run(tmp_path)
    with pytest.raises(PromoteError, match="no valid attestation"):
        promote(run, REGISTRY, REQUIRED, attestation=REJECT, project_path=proj(run))


def test_missing_receipt_is_refused(tmp_path):
    run = make_run(tmp_path)
    receipt_path(run, "train").unlink()
    with pytest.raises(PromoteError, match="no receipt"):
        promote(run, REGISTRY, REQUIRED, attestation=ACCEPT, project_path=proj(run))


def test_unknown_gate_name_is_refused(tmp_path):
    run = make_run(tmp_path)
    with pytest.raises(PromoteError, match="no such gate"):
        promote(run, REGISTRY, ["does_not_exist"], attestation=ACCEPT, project_path=proj(run))


# --- git attestation with real SSH signatures -------------------------------------------------

needs_ssh = pytest.mark.skipif(
    shutil.which("ssh-keygen") is None or shutil.which("git") is None,
    reason="git and ssh-keygen are needed for SSH attestation tests",
)


def _keypair(directory, name):
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", name, "-f", str(directory / name)],
        check=True,
    )
    return directory / name


def _allowed_signers(path, principal, pub_key):
    key_type, key_b64 = pub_key.read_text(encoding="utf-8").split()[:2]
    path.write_text(f"{principal} {key_type} {key_b64}\n", encoding="utf-8")
    return path


def _git(repo, *args, key=None):
    cmd = ["git", "-C", str(repo), "-c", "user.name=gdkit-test", "-c", "user.email=test@example.invalid"]
    if key is not None:
        cmd += ["-c", "gpg.format=ssh", "-c", f"user.signingkey={key}"]
    subprocess.run(cmd + list(args), check=True, capture_output=True, text=True)


@needs_ssh
def test_attestation_from_an_allowed_signer_is_accepted(tmp_path):
    repo = tmp_path / "attest-repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "runs" / RUN_ID).mkdir(parents=True)
    (repo / "runs" / RUN_ID / "ledger.jsonl").write_text("{}\n", encoding="utf-8")
    _git(repo, "add", "runs")
    _git(repo, "commit", "-q", "-m", "run commit")
    owner = _keypair(tmp_path, "owner")
    signers = _allowed_signers(tmp_path / "allowed_signers", "owner@gdkit", Path(str(owner) + ".pub"))
    digest = "d" * 64
    _git(repo, "tag", "-s", "attest/run-1", "-m", digest, key=owner)
    assert git_attestation(repo, signers)("run-1", digest) is True


@needs_ssh
def test_attestation_from_a_key_not_in_allowed_signers_is_refused(tmp_path):
    repo = tmp_path / "attest-repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "runs" / RUN_ID).mkdir(parents=True)
    (repo / "runs" / RUN_ID / "ledger.jsonl").write_text("{}\n", encoding="utf-8")
    _git(repo, "add", "runs")
    _git(repo, "commit", "-q", "-m", "run commit")
    owner = _keypair(tmp_path, "owner")
    intruder = _keypair(tmp_path, "intruder")
    signers = _allowed_signers(tmp_path / "allowed_signers", "owner@gdkit", Path(str(owner) + ".pub"))
    digest = "e" * 64
    _git(repo, "tag", "-s", "attest/run-1", "-m", digest, key=intruder)
    assert git_attestation(repo, signers)("run-1", digest) is False


# --- attestable_hash and the CI-facing CLI ---------------------------------------------------


def test_attestable_hash_returns_the_receipt_hash_for_an_expensive_gate(tmp_path):
    run = make_run(tmp_path)
    assert attestable_hash(run, "train", REGISTRY, project_path=proj(run)) == load_receipt(run, "train")["receipt_hash"]


def test_attestable_hash_refuses_a_cheap_gate(tmp_path):
    run = make_run(tmp_path)
    with pytest.raises(PromoteError, match="cheap gates"):
        attestable_hash(run, "row_count", REGISTRY, project_path=proj(run))


def test_attestable_hash_refuses_a_forged_expensive_receipt(tmp_path):
    run = make_run(tmp_path)
    rewrite_receipt(run, "train", lambda body: body.update(result={"loss": 0.0}))
    # The forged result is internally consistent, so the hash check passes. Attestation does not
    # re-run expensive gates, so this is exactly what the signature (not this function) must stop.
    assert attestable_hash(run, "train", REGISTRY, project_path=proj(run)) == load_receipt(run, "train")["receipt_hash"]
    rewrite_receipt(run, "train", lambda body: body.update(run_id="run-2"))
    with pytest.raises(PromoteError, match="another run"):
        attestable_hash(run, "train", REGISTRY, project_path=proj(run))


@needs_ssh
def test_tag_on_a_commit_without_the_run_is_refused(tmp_path):
    repo = tmp_path / "attest-repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "unrelated.txt").write_text("x", encoding="utf-8")
    _git(repo, "add", "unrelated.txt")
    _git(repo, "commit", "-q", "-m", "no run here")
    owner = _keypair(tmp_path, "owner")
    signers = _allowed_signers(tmp_path / "allowed_signers", "owner@gdkit", Path(str(owner) + ".pub"))
    digest = "f" * 64
    _git(repo, "tag", "-s", "attest/run-1", "-m", digest, key=owner)
    assert git_attestation(repo, signers)("run-1", digest) is False


def test_cli_attest_prints_the_hash_and_refuses_with_status_1(tmp_path):
    import os
    import sys

    run = make_run(tmp_path)
    root = Path(__file__).resolve().parents[1]
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(root), str(root / "tests"), str(root / "vendor" / "opengrad")])
    cmd = [sys.executable, "-m", "gdkit.cli", "--registry", "gates_fixture:REGISTRY", "attest", "--project", str(proj(run))]

    ok = subprocess.run(cmd + [str(run), "train"], capture_output=True, text=True, env=env, cwd=root)
    assert ok.returncode == 0, ok.stderr
    assert ok.stdout.strip() == load_receipt(run, "train")["receipt_hash"]

    refused = subprocess.run(cmd + [str(run), "row_count"], capture_output=True, text=True, env=env, cwd=root)
    assert refused.returncode == 1
    assert "cheap gates" in refused.stderr
