"""The issue log: events, state, dedup, links to runs, and the promote block."""

import hashlib
import json
from pathlib import Path

import pytest

from gates_fixture import REGISTRY
from gdkit.cards import model_card
from gdkit.gates import run_gate
from gdkit.issues import IssueError, IssueLog, fingerprint, main
from gdkit.ledger import Ledger
from gdkit.promote import PromoteError, attestable_hash, promote
from gdkit.run import open_ledger, record_config, record_file, start_run

SETUP = {"base_model_revision": "r", "tokenizer_sha256": "1" * 64, "config_sha256": "2" * 64,
         "eval_harness_sha256": "3" * 64, "prompt_template_sha256": "4" * 64}
ACCEPT = lambda run_id, gate, digest: True  # noqa: E731


def _project(root):
    (root / ".gdkit").mkdir(exist_ok=True)
    path = root / ".gdkit" / "project.yaml"
    path.write_text("study_question: q\n", encoding="utf-8")
    return path


def _promotable_run(root, run_id="r1"):
    """A registered run whose row_count and train receipts would promote, and its project file."""
    project = _project(root)
    run = start_run(root / "runs", run_id, mode="registered", project_path=project)
    ledger = open_ledger(run)
    record_config(ledger, run, SETUP)
    (run / "data.csv").write_text("a\nb\nc\n", encoding="utf-8")
    record_file(ledger, run, "load", "data.csv")
    head = (run / "ledger.jsonl").read_text(encoding="utf-8").splitlines()[-1]
    head = json.loads(head)["entry_hash"]
    for gate in REGISTRY.values():
        run_gate(run, gate, inputs=["data.csv"], run_id=run_id, ledger_head=head)
    return run, project


def _log(root):
    return IssueLog(root / ".gdkit" / "issues" / "log.jsonl")


def _promote(run, project):
    return promote(run, REGISTRY, ["row_count", "train"], attestation=ACCEPT, project_path=project)


# --- events and state ------------------------------------------------------------------------


@pytest.mark.parametrize("slug", ["Bad", "a--b", "-a", "a_b", "x\n", ""])
def test_slugs_are_lowercase_kebab_case(tmp_path, slug):
    with pytest.raises(IssueError, match="slug"):
        _log(tmp_path).observe(slug, kind="upstream", severity="cosmetic", summary="s")


def test_state_folds_the_lifecycle(tmp_path):
    log = _log(tmp_path)
    log.observe("tf-keys", kind="upstream", severity="blocking", summary="key names differ",
                package="transformers", version="4.44.2")
    log.triage("tf-keys", severity="degrading", note="only the attention writer")
    log.workaround("tf-keys", description="accept both names", commit="196b6f2")
    log.filed("tf-keys", url="https://github.com/huggingface/transformers/issues/1")
    log.resolve("tf-keys", resolution="fixed-upstream", resolved_version="4.45.0")
    issue = IssueLog(log.path).issues()["tf-keys"]
    assert issue["status"] == "resolved" and issue["severity"] == "degrading"
    assert issue["workaround"] == {"description": "accept both names", "commit": "196b6f2"}
    assert issue["issue_url"].endswith("/issues/1") and issue["resolved_version"] == "4.45.0"


def test_events_on_an_unknown_issue_and_bad_values_are_refused(tmp_path):
    log = _log(tmp_path)
    with pytest.raises(IssueError, match="no such issue"):
        log.triage("nope", severity="cosmetic")
    log.observe("a", kind="data", severity="cosmetic", summary="s")
    with pytest.raises(IssueError, match="http"):
        log.filed("a", url="not a url")
    with pytest.raises(IssueError, match="duplicate-of"):
        log.resolve("a", resolution="duplicate")
    with pytest.raises(IssueError, match="already recorded"):
        log.observe("a", kind="data", severity="cosmetic", summary="s")


def test_the_same_error_is_one_issue_with_two_occurrences(tmp_path):
    log = _log(tmp_path)
    log.observe("oom", kind="upstream", severity="degrading", summary="oom", package="torch",
                error="CUDA out of memory at 0x7f3a2c on /home/a/run1/x.py line 42")
    entry = log.observe("oom-again", kind="upstream", severity="degrading", summary="oom", package="torch",
                        error="CUDA out of memory at 0x11bb on C:\\Users\\b\\x.py line 7")
    assert entry["event"] == "observed_again" and entry["slug"] == "oom"
    assert log.issues()["oom"]["occurrences"] == 2 and "oom-again" not in log.issues()
    assert fingerprint("torch", "a 1") != fingerprint("numpy", "a 1")


def test_observing_links_to_the_run_head_without_writing_the_run_ledger(tmp_path):
    run, _ = _promotable_run(tmp_path)
    before = (run / "ledger.jsonl").read_bytes()
    head = json.loads(before.decode("utf-8").splitlines()[-1])
    _log(tmp_path).observe("x", kind="harness", severity="cosmetic", summary="s", run_dir=run, gate="train")
    issue = _log(tmp_path).issues()["x"]
    assert issue["observed_in"] == {"run_id": "r1", "ledger_seq": head["seq"], "entry_hash": head["entry_hash"],
                                    "gate": "train"}
    assert issue["affected_runs"] == ["r1"] and issue["affected_gates"] == ["train"]
    assert (run / "ledger.jsonl").read_bytes() == before


def test_evidence_is_stored_by_path_and_hash(tmp_path):
    (tmp_path / "trace.txt").write_text("boom\n", encoding="utf-8")
    log = _log(tmp_path)
    log.observe("e", kind="surprise", severity="cosmetic", summary="s", evidence=["trace.txt"], project_root=tmp_path)
    digest = hashlib.sha256((tmp_path / "trace.txt").read_bytes()).hexdigest()
    assert log.issues()["e"]["evidence"] == [{"path": "trace.txt", "sha256": digest}]
    with pytest.raises(IssueError, match="does not exist"):
        log.observe("f", kind="surprise", severity="cosmetic", summary="s", evidence=["gone.txt"],
                    project_root=tmp_path)


def test_a_tampered_log_is_refused(tmp_path):
    log = _log(tmp_path)
    log.observe("a", kind="data", severity="blocking", summary="s")
    log.workaround("a", description="patched")
    lines = log.path.read_text(encoding="utf-8").splitlines()
    lines[1] = lines[1].replace("patched", "PATCHED")
    log.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(IssueError, match="does not verify"):
        IssueLog(log.path)


def test_a_second_writer_is_detected(tmp_path):
    first, second = _log(tmp_path), _log(tmp_path)
    first.observe("a", kind="data", severity="cosmetic", summary="s")
    with pytest.raises(IssueError, match="changed on disk"):
        second.observe("b", kind="data", severity="cosmetic", summary="s")


# --- the promote block -----------------------------------------------------------------------


def test_no_log_means_no_block_and_the_report_says_so(tmp_path):
    run, project = _promotable_run(tmp_path)
    assert _promote(run, project)["issues_log_head"] is None


def test_a_blocking_issue_on_the_run_blocks_promote_and_attest_until_a_workaround(tmp_path, capsys):
    run, project = _promotable_run(tmp_path)
    log_path = tmp_path / ".gdkit" / "issues" / "log.jsonl"
    assert main(["--log", str(log_path), "observe", "tf-keys", "--kind", "upstream", "--severity", "blocking",
                 "--summary", "key names differ", "--run", str(run)]) == 0
    with pytest.raises(PromoteError, match="issue tf-keys is blocking with no workaround.*workaround tf-keys"):
        _promote(run, project)
    with pytest.raises(PromoteError, match="tf-keys"):
        attestable_hash(run, "train", REGISTRY, project_path=project)
    assert main(["--log", str(log_path), "workaround", "tf-keys", "--description", "accept both names"]) == 0
    capsys.readouterr()
    report = _promote(run, project)
    assert report["issues_log_head"] == _log(tmp_path).head


def _printed_commands(message):
    """The clearing commands a refusal prints, as argument lists for gdkit.issues.main."""
    tail = message.split("Clear it with one of: ", 1)[1]
    return [part.strip().split()[3:] for part in tail.split(" ; ")]


def test_a_degrading_issue_blocks_until_it_is_triaged_with_the_printed_command(tmp_path, capsys):
    run, project = _promotable_run(tmp_path)
    log_path = _log(tmp_path).path
    _log(tmp_path).observe("drift", kind="data", severity="degrading", summary="s", affected_runs=["r1"])
    with pytest.raises(PromoteError, match="degrading and not triaged") as info:
        _promote(run, project)
    commands = _printed_commands(str(info.value))
    assert commands[0] == ["triage", "drift"] and "workaround" not in str(info.value)
    assert main(["--log", str(log_path), *commands[0]]) == 0
    _promote(run, project)


def test_the_printed_triage_command_clears_a_blocking_issue(tmp_path, capsys):
    run, project = _promotable_run(tmp_path)
    log_path = _log(tmp_path).path
    _log(tmp_path).observe("hard", kind="harness", severity="blocking", summary="s", affected_runs=["r1"])
    with pytest.raises(PromoteError) as info:
        _promote(run, project)
    triage = [c for c in _printed_commands(str(info.value)) if c[0] == "triage"][0]
    assert triage == ["triage", "hard", "--severity", "degrading"]
    assert main(["--log", str(log_path), *triage]) == 0
    _promote(run, project)


def test_a_reopened_issue_drops_its_old_workaround_and_blocks_again(tmp_path):
    run, project = _promotable_run(tmp_path)
    log = _log(tmp_path)
    log.observe("seg", kind="upstream", severity="blocking", summary="s", affected_runs=["r1"], error="segfault")
    log.workaround("seg", description="pin the old kernel")
    log.resolve("seg", resolution="adopted")
    log.again("seg", run_dir=run)
    issue = log.issues()["seg"]
    assert issue["workaround"] is None and issue["previous_workaround"]["description"] == "pin the old kernel"
    with pytest.raises(PromoteError, match="issue seg is blocking with no workaround"):
        _promote(run, project)
    log.workaround("seg", description="pin the old kernel again")
    _promote(run, project)


def test_a_resolved_issue_seen_again_reopens_and_blocks(tmp_path):
    run, project = _promotable_run(tmp_path)
    log = _log(tmp_path)
    log.observe("seg", kind="upstream", severity="blocking", summary="s", affected_runs=["r1"], error="segfault")
    log.resolve("seg", resolution="fixed-upstream")
    _promote(run, project)
    log.observe("seg-2", kind="upstream", severity="blocking", summary="s", error="segfault")
    assert log.issues()["seg"]["status"] == "observed" and log.issues()["seg"]["reopened"] == 1
    with pytest.raises(PromoteError, match="issue seg is blocking"):
        _promote(run, project)


@pytest.mark.parametrize(
    "where, blocks",
    [
        ({"affected_runs": ["other-run"]}, False),
        ({"affected_gates": ["train"]}, True),
        ({"affected_gates": ["unrelated"]}, False),
        ({}, True),  # names neither run nor gate: project-wide
    ],
)
def test_which_runs_and_gates_an_issue_blocks(tmp_path, where, blocks):
    run, project = _promotable_run(tmp_path)
    _log(tmp_path).observe("i", kind="harness", severity="blocking", summary="s", **where)
    if blocks:
        with pytest.raises(PromoteError, match="issue i"):
            _promote(run, project)
    else:
        _promote(run, project)


def test_cosmetic_issues_never_block(tmp_path):
    run, project = _promotable_run(tmp_path)
    _log(tmp_path).observe("c", kind="surprise", severity="cosmetic", summary="s")
    _promote(run, project)


def test_a_damaged_issue_log_refuses_promote(tmp_path):
    run, project = _promotable_run(tmp_path)
    log = _log(tmp_path)
    log.observe("a", kind="data", severity="cosmetic", summary="s")
    log.path.write_text(log.path.read_text(encoding="utf-8").replace("cosmetic", "blocking"), encoding="utf-8")
    with pytest.raises(PromoteError, match="does not verify"):
        _promote(run, project)


def test_the_model_card_lists_issues_affecting_the_run(tmp_path):
    run, _ = _promotable_run(tmp_path)
    log = _log(tmp_path)
    log.observe("mine", kind="harness", severity="blocking", summary="s", affected_runs=["r1"])
    log.workaround("mine", description="w")
    log.observe("theirs", kind="harness", severity="blocking", summary="s", affected_runs=["r9"])
    card = model_card(run, issues_path=log.path)
    assert card["issues_log_head"] == log.head
    assert [i["slug"] for i in card["issues"]] == ["mine"] and card["issues"][0]["blocks"] is None
    assert model_card(run)["issues"] is None  # not checked, which is not the same as none


def test_the_issue_log_is_not_mistaken_for_an_explore_run(tmp_path):
    _log(tmp_path).observe("a", kind="data", severity="cosmetic", summary="s")
    run = start_run(tmp_path / ".gdkit", "r1", mode="registered", project_path=_project(tmp_path))
    first = json.loads((run / "ledger.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert first["params"]["explore_runs_checked"] == []


def test_the_entry_points_run_as_real_processes(tmp_path):
    """python -m gdkit.run and python -m gdkit.issues, outside pytest's process."""
    import os
    import subprocess
    import sys

    root = Path(__file__).resolve().parents[1]
    env = {**os.environ, "PYTHONPATH": str(root)}
    run, _ = _promotable_run(tmp_path)

    def call(*args):
        proc = subprocess.run([sys.executable, "-m", *args], capture_output=True, text=True, env=env, cwd=tmp_path)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        return json.loads(proc.stdout)

    assert call("gdkit.run", "host", str(run), "--package", "pyyaml")["op"] == "host"
    assert call("gdkit.run", "audit", str(run))["ok"] is True
    assert call("gdkit.issues", "observe", "a", "--kind", "data", "--severity", "cosmetic", "--summary", "s",
                "--run", str(run))["status"] == "observed"
    assert call("gdkit.issues", "list")["issues"][0]["slug"] == "a"


def test_the_cli_lists_issues_with_what_blocks(tmp_path, capsys):
    log_path = tmp_path / "log.jsonl"
    main(["--log", str(log_path), "observe", "a", "--kind", "data", "--severity", "blocking", "--summary", "s"])
    capsys.readouterr()
    assert main(["--log", str(log_path), "list"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["issues"] == [{"slug": "a", "kind": "data", "severity": "blocking", "status": "observed",
                              "occurrences": 1, "workaround": False, "blocks": "blocking with no workaround"}]
    assert main(["--log", str(log_path), "triage", "nope"]) == 1
