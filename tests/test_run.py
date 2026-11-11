"""Run recording: start, host, scope/config/decisions, and the run CLI."""

import getpass
import hashlib
import json
import platform
import sys
from pathlib import Path

import pytest
import yaml

from gates_fixture import COUNT, REGISTRY
from gdkit.cards import CardError, model_card
from gdkit.gates import run_gate
from gdkit.host import record_host
from gdkit.ledger import verify
from gdkit.promote import PromoteError, promote
from gdkit.run import (
    RunError,
    main,
    open_ledger,
    record_config,
    record_decision,
    record_file,
    record_scope,
    start_run,
)

SETUP = {
    "base_model_revision": "15852e8c",
    "tokenizer_sha256": "1" * 64,
    "config_sha256": "2" * 64,
    "eval_harness_sha256": "3" * 64,
    "prompt_template_sha256": "4" * 64,
}


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _entries(run):
    return [json.loads(line) for line in (run / "ledger.jsonl").read_text(encoding="utf-8").splitlines()]


def _project(root, answers=None):
    (root / ".gdkit").mkdir(exist_ok=True)
    path = root / ".gdkit" / "project.yaml"
    path.write_text(yaml.safe_dump(answers or {"study_question": "q", "baseline": "b"}), encoding="utf-8")
    return path


def _start(root, run_id="r1", mode="registered", **kw):
    return start_run(root / "runs", run_id, mode=mode, project_path=_project(root, kw.pop("answers", None)), **kw)


# --- start -----------------------------------------------------------------------------------


def test_start_freezes_first_then_records_the_run_file(tmp_path):
    run = _start(tmp_path, label="pilot")
    entries = _entries(run)
    assert [e["op"] for e in entries] == ["preregistration", "run_open"]
    meta = json.loads((run / "run.json").read_text(encoding="utf-8"))
    assert meta["run_id"] == "r1" and meta["mode"] == "registered" and meta["label"] == "pilot"
    assert meta["project_file"] == ".gdkit/project.yaml" and meta["runs_dir"] == "runs"
    assert entries[1]["event"] is True
    assert entries[1]["params"]["sha256"] == _sha(run / "run.json") == entries[1]["output_hash"]
    assert (run / ".gitignore").read_text(encoding="utf-8").split() == ["host.json", "logs/"]


def test_start_refuses_a_run_that_already_started(tmp_path):
    _start(tmp_path)
    with pytest.raises(RunError, match="already started"):
        _start(tmp_path)


@pytest.mark.parametrize("bad", ["", "a/b", "../x", "has space", "r1\n"])
def test_start_refuses_an_unsafe_run_id(tmp_path, bad):
    with pytest.raises(RunError, match="run id"):
        _start(tmp_path, run_id=bad)


def test_a_refused_freeze_can_be_retried_after_declaring(tmp_path, capsys):
    """The cross-check refuses; declare through the tool; start again through the CLI."""
    root = tmp_path
    _start(root, run_id="explore-1", mode="explore", answers={"baseline": "a"})
    project = _project(root, {"baseline": "b"})
    args = ["start", "--runs-dir", str(root / "runs"), "--run-id", "reg-1", "--mode", "registered",
            "--project", str(project)]
    assert main(args) == 1
    refused = json.loads(capsys.readouterr().out)
    assert refused["ok"] is False and "declare --id baseline" in refused["message"]

    from gdkit.skill_api import main as skill_main

    questions = Path(__file__).resolve().parents[1] / "interview" / "questions.yaml"
    assert skill_main(["declare", "--project", str(project), "--questions", str(questions),
                       "--id", "baseline", "--list", "changed_after_explore"]) == 0
    capsys.readouterr()
    assert main(args) == 0
    started = json.loads(capsys.readouterr().out)
    assert started["ok"] is True and Path(started["run_dir"]).name == "reg-1"


# --- side files ------------------------------------------------------------------------------


def test_scope_config_and_decisions_are_written_as_exact_bytes_and_hashed(tmp_path):
    run = _start(tmp_path)
    ledger = open_ledger(run)
    record_scope(ledger, run, {"model": "m", "notes": "line one\nline two"})
    record_config(ledger, run, SETUP)
    first = record_decision(ledger, run, {"decision": "drop slice C", "why": "empty"})
    for rel in ("scope.json", "config.json", first["params"]["path"]):
        data = (run / rel).read_bytes()
        assert b"\r\n" not in data  # written as bytes, so Windows does not turn LF into CRLF
    entries = _entries(run)
    assert [e["op"] for e in entries[2:]] == ["scope", "config", "decision"]
    for entry in entries[2:]:
        assert entry["params"]["sha256"] == _sha(run / entry["params"]["path"]) == entry["input_hash"]
    assert first["params"]["path"] == "decisions/000004.json"


def test_record_file_hashes_a_file_already_in_the_run(tmp_path):
    run = _start(tmp_path)
    (run / "splits").mkdir()
    (run / "splits" / "train.jsonl").write_bytes(b'{"a": 1}\n')
    entry = record_file(open_ledger(run), run, "split_build", "splits/train.jsonl")
    assert entry["params"] == {"path": "splits/train.jsonl", "sha256": _sha(run / "splits" / "train.jsonl"), "bytes": 9}


def test_explore_runs_keep_hashes_in_params(tmp_path):
    run = _start(tmp_path, mode="explore")
    entry = record_scope(open_ledger(run), run, {"model": "m"})
    assert entry["input_hash"] is None and entry["params"]["sha256"] == _sha(run / "scope.json")
    assert verify(run / "ledger.jsonl") == 3


def test_record_file_refuses_a_path_outside_the_run(tmp_path):
    run = _start(tmp_path)
    (tmp_path / "outside.txt").write_text("x", encoding="utf-8")
    with pytest.raises(RunError, match="inside the run"):
        record_file(open_ledger(run), run, "load", "../outside.txt")


# --- host ------------------------------------------------------------------------------------


def test_the_host_record_keeps_private_details_out_of_the_ledger(tmp_path):
    run = _start(tmp_path)
    probe = [sys.executable, "-c", "print('GPU-1234 on secret-host')"]
    entry = record_host(open_ledger(run), run, packages=["pyyaml"], probes=[probe])
    full = json.loads((run / "host.json").read_text(encoding="utf-8"))
    assert "GPU-1234 on secret-host" in full["probes"][0]["stdout"]
    assert full["platform"]["node"] == platform.node()
    summary = entry["params"]["summary"]
    assert summary["packages"]["pyyaml"] and "gdkit" in summary["packages"]
    assert summary["probes"][0]["stdout_sha256"] and "stdout" not in summary["probes"][0]
    assert entry["params"]["sha256"] == _sha(run / "host.json")

    ledger_text = (run / "ledger.jsonl").read_text(encoding="utf-8")
    home = Path.home()
    for private in (getpass.getuser(), platform.node(), json.dumps(str(home))[1:-1], home.as_posix(),
                    sys.executable, "secret-host"):
        assert private not in ledger_text, private


def test_a_missing_probe_is_recorded_as_unavailable(tmp_path):
    run = _start(tmp_path)
    entry = record_host(open_ledger(run), run, probes=[["no-such-probe-xyz", "--version"]])
    assert entry["params"]["summary"]["probes"] == [
        {"name": "no-such-probe-xyz", "available": False, "returncode": None, "stdout_sha256": None}
    ]


# --- receipts stay fresh, cards use config.json ----------------------------------------------


def _full_run(tmp_path):
    run = _start(tmp_path, answers={"study_question": "q"})
    ledger = open_ledger(run)
    record_host(ledger, run)
    record_scope(ledger, run, {"model": "m"})
    record_config(ledger, run, SETUP)
    (run / "data.csv").write_text("a\nb\nc\n", encoding="utf-8")
    record_file(ledger, run, "load", "data.csv")
    run_gate(run, COUNT, inputs=["data.csv"], run_id="r1", ledger_head=_entries(run)[-1]["entry_hash"])
    return run, ledger


def test_receipts_stay_fresh_after_a_full_start_to_gate_sequence(tmp_path):
    run, ledger = _full_run(tmp_path)
    project = tmp_path / ".gdkit" / "project.yaml"
    assert promote(run, REGISTRY, ["row_count"], project_path=project)["gates"]["row_count"] == "re-run and matched"
    record_decision(ledger, run, {"decision": "late"})  # anything after the gate makes its receipt stale
    with pytest.raises(PromoteError, match="stale"):
        promote(run, REGISTRY, ["row_count"], project_path=project)


def test_the_model_card_reads_config_json_and_names_the_recorded_files(tmp_path):
    run, _ = _full_run(tmp_path)
    card = model_card(run)
    assert card["base_model_revision"] == "15852e8c"
    entries = {e["op"]: e for e in _entries(run)}
    assert card["config_sha256_recorded"] == entries["config"]["params"]["sha256"]
    assert card["scope_sha256"] == entries["scope"]["params"]["sha256"]
    assert card["host_sha256"] == entries["host"]["params"]["sha256"]
    assert card["decisions"] == []


def test_the_model_card_refuses_a_config_changed_after_it_was_recorded(tmp_path):
    run, _ = _full_run(tmp_path)
    (run / "config.json").write_text(json.dumps({**SETUP, "base_model_revision": "other"}), encoding="utf-8")
    with pytest.raises(CardError, match="config.json has changed"):
        model_card(run)


# --- CLI -------------------------------------------------------------------------------------


def test_the_cli_records_host_scope_and_audits(tmp_path, capsys):
    run = _start(tmp_path)
    assert main(["host", str(run), "--package", "pyyaml"]) == 0
    scope = tmp_path / "scope_in.json"
    scope.write_text(json.dumps({"model": "m"}), encoding="utf-8")
    assert main(["scope", str(run), "--json", str(scope)]) == 0
    assert main(["decide", str(run), "--json", str(scope)]) == 0
    capsys.readouterr()
    assert main(["audit", str(run)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] is True and report["report"]["ledger"]["entries"] == 5
