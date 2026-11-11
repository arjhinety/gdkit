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
ACCEPT = lambda run_id, gate, digest: True  # noqa: E731
REJECT = lambda run_id, gate, digest: False  # noqa: E731


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
        json.dumps({"run_id": RUN_ID, "gate": "row_count", "result": {"passed": True, "rows": 3}, "receipt_hash": "0" * 64}),
        encoding="utf-8",
    )
    with pytest.raises(PromoteError, match="does not match its body"):
        promote(run, REGISTRY, REQUIRED, attestation=ACCEPT, project_path=proj(run))


def test_forged_result_with_recomputed_hash_fails_the_rerun(tmp_path):
    run = make_run(tmp_path)
    rewrite_receipt(run, "row_count", lambda body: body.update(result={"passed": True, "rows": 99}))
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


def _sign(repo, run_id, gate, digest, key, head="none"):
    """Sign an attestation tag the way the workflow does: per-receipt name, hash subject, issues-head body."""
    _git(repo, "tag", "-s", f"attest/{run_id}/{gate}/{digest[:16]}", "-m", digest, "-m", f"issues-head: {head}",
         key=key)


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
    _sign(repo, "run-1", "train", digest, owner)
    assert git_attestation(repo, signers)("run-1", "train", digest) == {"ok": True, "issues_head": "none"}


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
    _sign(repo, "run-1", "train", digest, intruder)
    assert git_attestation(repo, signers)("run-1", "train", digest) is False


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
    rewrite_receipt(run, "train", lambda body: body.update(result={"passed": True, "loss": 0.0}))
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
    _sign(repo, "run-1", "train", digest, owner)
    assert git_attestation(repo, signers)("run-1", "train", digest) is False


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


# --- review fixes: gate pass flag, one attestation tag per gate --------------------------------


def _failing_gate():
    from gates_fixture import failing_leak_check

    return Gate("leak_check", cheap=True, check=failing_leak_check)


def _head(run):
    return json.loads((run / "ledger.jsonl").read_text(encoding="utf-8").splitlines()[-1])["entry_hash"]


def test_a_cheap_gate_that_reported_failure_is_refused(tmp_path):
    run = make_run(tmp_path)
    gate = _failing_gate()
    run_gate(run, gate, inputs=[], run_id=RUN_ID, ledger_head=_head(run))
    registry = {**REGISTRY, "leak_check": gate}
    with pytest.raises(PromoteError, match="leak_check: the gate did not pass"):
        promote(run, registry, ["leak_check"], attestation=ACCEPT, project_path=proj(run))


def test_an_expensive_receipt_that_reported_failure_is_not_attestable(tmp_path):
    run = make_run(tmp_path)
    rewrite_receipt(run, "train", lambda body: body.update(result={"passed": False, "loss": 9.0}))
    with pytest.raises(PromoteError, match="train: the gate did not pass"):
        attestable_hash(run, "train", REGISTRY, project_path=proj(run))
    with pytest.raises(PromoteError, match="train: the gate did not pass"):
        promote(run, REGISTRY, REQUIRED, attestation=ACCEPT, project_path=proj(run))


@pytest.mark.parametrize("result", [{"rows": 3}, {"passed": "true"}, {"passed": 1}, [True], None])
def test_a_receipt_without_a_true_pass_flag_is_refused(tmp_path, result):
    run = make_run(tmp_path)
    rewrite_receipt(run, "row_count", lambda body: body.update(result=result))
    with pytest.raises(PromoteError, match="row_count: the gate did not pass"):
        promote(run, REGISTRY, REQUIRED, attestation=ACCEPT, project_path=proj(run))


@pytest.mark.parametrize("result", [{"rows": 3}, {"passed": "yes"}, "ok", None])
def test_run_gate_refuses_a_result_without_a_boolean_pass_flag(tmp_path, result):
    from gdkit.gates import GateError

    run = tmp_path / "run"
    run.mkdir()
    gate = Gate("odd", cheap=True, check=lambda run_dir: result)
    with pytest.raises(GateError, match="passed"):
        run_gate(run, gate, inputs=[], run_id=RUN_ID, ledger_head="0" * 64)
    assert not (run / "receipts" / "odd.json").exists()


@pytest.mark.parametrize("name", ["", "../escape", "a/b", "has space", "dot.name", "train\n"])
def test_a_gate_name_must_be_safe_for_paths_and_tags(name):
    from gdkit.gates import GateError

    with pytest.raises(GateError):
        Gate(name, cheap=True, check=lambda run_dir: {"passed": True})


def _attest_repo(tmp_path):
    repo = tmp_path / "attest-repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "runs" / RUN_ID).mkdir(parents=True)
    (repo / "runs" / RUN_ID / "ledger.jsonl").write_text("{}\n", encoding="utf-8")
    _git(repo, "add", "runs")
    _git(repo, "commit", "-q", "-m", "run commit")
    owner = _keypair(tmp_path, "owner")
    signers = _allowed_signers(tmp_path / "allowed_signers", "owner@gdkit", Path(str(owner) + ".pub"))
    return repo, owner, signers


@needs_ssh
def test_two_expensive_gates_in_one_run_are_attested_separately(tmp_path):
    repo, owner, signers = _attest_repo(tmp_path)
    train_digest, judge_digest = "1" * 64, "2" * 64
    _sign(repo, RUN_ID, "train", train_digest, owner)
    _sign(repo, RUN_ID, "judge", judge_digest, owner)
    verify = git_attestation(repo, signers)
    assert verify(RUN_ID, "train", train_digest)["ok"] is True
    assert verify(RUN_ID, "judge", judge_digest)["ok"] is True
    assert verify(RUN_ID, "train", judge_digest) is False  # one gate's approval does not cover another


@needs_ssh
def test_a_tag_whose_message_only_contains_the_digest_is_refused(tmp_path):
    repo, owner, signers = _attest_repo(tmp_path)
    digest = "3" * 64
    _git(repo, "tag", "-s", f"attest/{RUN_ID}/train/{digest[:16]}", "-m", f"not approved: {digest}",
         "-m", "issues-head: none", key=owner)
    assert git_attestation(repo, signers)(RUN_ID, "train", digest) is False


# --- slice 5: one tag per receipt, and the signed issue log head -------------------------------


def _run_with_issue_log(tmp_path):
    """A run whose train receipt can be attested, a project issue log with one entry, and a signing repo."""
    from gdkit.issues import IssueLog

    run = make_run(tmp_path)
    log = IssueLog(tmp_path / "issues.jsonl")
    log.observe("seen", kind="data", severity="cosmetic", summary="seen by the reviewer")
    repo, owner, signers = _attest_repo(tmp_path)
    digest = load_receipt(run, "train")["receipt_hash"]
    return run, log, repo, owner, signers, digest


def _promote_train(run, repo, signers, log_path):
    return promote(run, REGISTRY, ["train"], attestation=git_attestation(repo, signers), project_path=proj(run),
                   issues_path=log_path)


@needs_ssh
def test_a_signed_head_still_in_the_log_promotes_even_after_the_log_grew(tmp_path):
    run, log, repo, owner, signers, digest = _run_with_issue_log(tmp_path)
    signed = log.head
    _sign(repo, RUN_ID, "train", digest, owner, head=signed)
    log.observe("later", kind="data", severity="cosmetic", summary="found after approval")
    report = _promote_train(run, repo, signers, log.path)
    assert report["attestations"]["train"] == {"issues_head_signed": signed, "issues_head_checked": True}
    assert signed != log.head  # the log grew after the approval


@needs_ssh
def test_a_rewritten_or_deleted_issue_log_refuses_the_signed_approval(tmp_path):
    from gdkit.issues import IssueLog

    run, log, repo, owner, signers, digest = _run_with_issue_log(tmp_path)
    _sign(repo, RUN_ID, "train", digest, owner, head=log.head)
    log.path.unlink()
    rewritten = IssueLog(log.path)
    rewritten.observe("other", kind="data", severity="cosmetic", summary="a different history")
    with pytest.raises(PromoteError, match="not in the current issue log"):
        _promote_train(run, repo, signers, log.path)
    log.path.unlink()
    with pytest.raises(PromoteError, match="the issue log .* is gone"):
        _promote_train(run, repo, signers, log.path)


@needs_ssh
def test_none_signed_then_a_log_created_still_promotes(tmp_path):
    from gdkit.issues import IssueLog

    run = make_run(tmp_path)
    repo, owner, signers = _attest_repo(tmp_path)
    _sign(repo, RUN_ID, "train", load_receipt(run, "train")["receipt_hash"], owner, head="none")
    log = IssueLog(tmp_path / "issues.jsonl")
    log.observe("new", kind="data", severity="cosmetic", summary="after approval")
    assert _promote_train(run, repo, signers, log.path)["attestations"]["train"]["issues_head_signed"] == "none"


@needs_ssh
@pytest.mark.parametrize(
    "body",
    [[], ["issues-head: none", "issues-head: none"], ["issues-head: NONE"], ["issues-head: abc"],
     ["issues-head: none", "extra line"]],
)
def test_a_tag_without_exactly_one_valid_issues_head_line_is_refused(tmp_path, body):
    repo, owner, signers = _attest_repo(tmp_path)
    digest = "4" * 64
    args = ["tag", "-s", f"attest/{RUN_ID}/train/{digest[:16]}", "-m", digest]
    for line in body:
        args += ["-m", line]
    _git(repo, *args, key=owner)
    assert git_attestation(repo, signers)(RUN_ID, "train", digest) is False


@needs_ssh
def test_a_re_run_gate_gets_its_own_tag_and_the_old_approval_stays(tmp_path):
    repo, owner, signers = _attest_repo(tmp_path)
    first, second = "5" * 64, "6" * 64
    _sign(repo, RUN_ID, "train", first, owner)
    _sign(repo, RUN_ID, "train", second, owner)  # would fail if the tag name were per gate
    verify = git_attestation(repo, signers)
    assert verify(RUN_ID, "train", first)["ok"] and verify(RUN_ID, "train", second)["ok"]


def test_a_plain_bool_verifier_is_reported_as_not_checking_the_issue_head(tmp_path):
    run = make_run(tmp_path)
    report = promote(run, REGISTRY, REQUIRED, attestation=ACCEPT, project_path=proj(run))
    assert report["attestations"]["train"] == {"issues_head_signed": None, "issues_head_checked": False}


def test_the_cli_prints_the_issue_log_head(tmp_path, capsys):
    from gdkit.cli import main
    from gdkit.issues import IssueLog

    root = tmp_path / "study"
    (root / ".gdkit").mkdir(parents=True)
    project = root / ".gdkit" / "project.yaml"
    project.write_text("study_question: q\n", encoding="utf-8")
    assert main(["--registry", "gates_fixture:REGISTRY", "issues-head", "--project", str(project)]) == 0
    assert capsys.readouterr().out.strip() == "none"
    log = IssueLog(root / ".gdkit" / "issues" / "log.jsonl")
    log.observe("a", kind="data", severity="cosmetic", summary="s")
    assert main(["--registry", "gates_fixture:REGISTRY", "issues-head", "--project", str(project)]) == 0
    assert capsys.readouterr().out.strip() == log.head
