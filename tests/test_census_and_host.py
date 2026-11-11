"""Census rules, and the same-machine check in run_gate."""

import json
import sys
from pathlib import Path

import pytest

from gdkit.census import (
    CONDITIONALLY_REQUIRED,
    OPTIONAL,
    REQUIRED_NONEMPTY,
    Census,
    CensusError,
    validate_census_dict,
)
from gdkit.gates import Gate, GateError, receipt_hash, run_gate
from gdkit.host import record_host
from gdkit.promote import PromoteError, attestable_hash, promote
from gdkit.run import open_ledger, record_file, start_run

ACCEPT = lambda run_id, gate, digest: True  # noqa: E731


# --- census ----------------------------------------------------------------------------------


def test_counts_are_literal_and_add_up():
    census = Census("split_integrity")
    for arm in ("a", "b", "c", "d", "e"):
        census.discover(arm)
    census.passed_item("a")
    census.passed_item("b")
    census.failed_item("c", "hash mismatch")
    census.skipped_item("d", "arm not built for this model")
    census.blocked_item("e", "upstream gate has not run")
    census.validate()
    d = census.to_dict()
    assert (d["discovered"], d["checked"], d["passed"], d["failed"], d["skipped"], d["blocked"]) == (5, 3, 2, 1, 1, 1)
    assert d["failures"] == [{"item": "c", "reason": "hash mismatch"}]


def test_item_level_mistakes_are_refused_even_when_totals_could_add_up():
    census = Census("g")
    with pytest.raises(CensusError, match="never discovered"):
        census.passed_item("ghost")
    census.discover("a")
    census.passed_item("a")
    with pytest.raises(CensusError, match="already has the outcome"):
        census.failed_item("a", "second outcome")
    with pytest.raises(CensusError, match="discovered twice"):
        census.discover("a")
    census.discover("b")
    with pytest.raises(CensusError, match="without an outcome"):
        census.validate()
    with pytest.raises(CensusError, match="without a reason"):
        census.skipped_item("b", " ")


@pytest.mark.parametrize(
    "policy, precondition, refused",
    [(REQUIRED_NONEMPTY, True, True), (CONDITIONALLY_REQUIRED, True, True),
     (CONDITIONALLY_REQUIRED, False, False), (OPTIONAL, True, False)],
)
def test_empty_populations_follow_the_policy(policy, precondition, refused):
    census = Census("g", policy, precondition_met=precondition)
    if refused:
        with pytest.raises(CensusError, match="required but empty"):
            census.validate()
    else:
        census.validate()


def test_a_stored_census_that_does_not_add_up_is_refused():
    good = Census("g")
    good.discover("a")
    good.passed_item("a")
    data = good.to_dict()
    validate_census_dict(data)
    with pytest.raises(CensusError, match="checked"):
        validate_census_dict({**data, "passed": 0})
    with pytest.raises(CensusError, match="failures lists"):
        validate_census_dict({**data, "checked": 2, "failed": 1, "discovered": 2})


# --- census in run_gate and promote ----------------------------------------------------------


def _counting_check(run_dir):
    census = Census("rows")  # fresh on every call
    for line in (Path(run_dir) / "data.csv").read_text(encoding="utf-8").splitlines():
        census.discover(line)
        census.passed_item(line)
    return {"passed": census.failed == 0, "census": census.to_dict()}


def _lying_check(run_dir):
    census = Census("rows")
    census.discover("a")
    census.failed_item("a", "bad row")
    return {"passed": True, "census": census.to_dict()}


def _project_run(tmp_path, *, host=False):
    (tmp_path / ".gdkit").mkdir()
    project = tmp_path / ".gdkit" / "project.yaml"
    project.write_text("study_question: q\n", encoding="utf-8")
    run = start_run(tmp_path / "runs", "r1", mode="registered", project_path=project)
    ledger = open_ledger(run)
    if host:
        record_host(ledger, run, probes=[[sys.executable, "-c", "print('gpu-model-x')"]])
    (run / "data.csv").write_text("a\nb\nc\n", encoding="utf-8")
    record_file(ledger, run, "load", "data.csv")
    return run, project


def _head(run):
    return json.loads((run / "ledger.jsonl").read_text(encoding="utf-8").splitlines()[-1])["entry_hash"]


def test_a_census_in_the_result_is_kept_and_promote_re_runs_it_twice(tmp_path):
    run, project = _project_run(tmp_path)
    gate = Gate("rows", cheap=True, check=_counting_check)
    path = run_gate(run, gate, inputs=["data.csv"], run_id="r1", ledger_head=_head(run))
    assert json.loads(path.read_text(encoding="utf-8"))["result"]["census"]["passed"] == 3
    for _ in range(2):
        promote(run, {"rows": gate}, ["rows"], project_path=project)


def test_a_result_that_passes_with_census_failures_gets_no_receipt(tmp_path):
    run, _ = _project_run(tmp_path)
    with pytest.raises(GateError, match="passed but its census has 1 failure"):
        run_gate(run, Gate("liar", cheap=True, check=_lying_check), inputs=[], run_id="r1", ledger_head=_head(run))
    assert not (run / "receipts" / "liar.json").exists()


def test_promote_refuses_a_stored_census_edited_to_hide_a_failure(tmp_path):
    run, project = _project_run(tmp_path)
    gate = Gate("rows", cheap=False, check=_counting_check)
    path = run_gate(run, gate, inputs=["data.csv"], run_id="r1", ledger_head=_head(run))
    body = json.loads(path.read_text(encoding="utf-8"))
    body["result"]["census"]["failed"] = 1  # counts no longer add up
    body["receipt_hash"] = receipt_hash(body)
    path.write_text(json.dumps(body), encoding="utf-8")
    with pytest.raises(PromoteError, match="rows: census"):
        promote(run, {"rows": gate}, ["rows"], attestation=ACCEPT, project_path=project)


# --- the same-machine check ------------------------------------------------------------------

COUNT = Gate("count", cheap=True, check=lambda run_dir: {"passed": True})


def test_a_run_without_a_host_entry_records_that_the_check_did_not_run(tmp_path):
    run, _ = _project_run(tmp_path)
    body = json.loads(run_gate(run, COUNT, inputs=[], run_id="r1", ledger_head=_head(run)).read_text(encoding="utf-8"))
    assert body["host_check"] == {"checked": False, "reason": "the run has no host entry"}


def test_the_same_machine_passes_and_the_receipt_names_the_host_record(tmp_path):
    run, _ = _project_run(tmp_path, host=True)
    host_sha = [json.loads(line) for line in (run / "ledger.jsonl").read_text(encoding="utf-8").splitlines()
                if '"op":"host"' in line][0]["params"]["sha256"]
    body = json.loads(run_gate(run, COUNT, inputs=[], run_id="r1", ledger_head=_head(run)).read_text(encoding="utf-8"))
    assert body["host_check"] == {"checked": True, "host_sha256": host_sha}


def test_a_different_machine_is_refused_and_told_how_to_re_record(tmp_path, monkeypatch):
    import gdkit.host as host

    run, _ = _project_run(tmp_path, host=True)
    real = host.collect_host

    def other_machine(**kwargs):
        full = real(**kwargs)
        full["platform"]["machine"] = "aarch64-elsewhere"
        return full

    monkeypatch.setattr(host, "collect_host", other_machine)
    with pytest.raises(GateError, match=r"platform\.machine: recorded .* now 'aarch64-elsewhere'.*gdkit\.run host"):
        run_gate(run, COUNT, inputs=[], run_id="r1", ledger_head=_head(run))
    assert not (run / "receipts" / "count.json").exists()


def test_a_changed_probe_output_is_named(tmp_path, monkeypatch):
    run, _ = _project_run(tmp_path, host=True)
    full = json.loads((run / "host.json").read_text(encoding="utf-8"))
    import gdkit.host as host

    real = host._probe

    def other_gpu(command, timeout=30):
        result = real(command, timeout)
        result["stdout"] = "gpu-model-y"
        return result

    monkeypatch.setattr(host, "_probe", other_gpu)
    with pytest.raises(GateError, match=r"probes\[0\]\.stdout_sha256"):
        run_gate(run, COUNT, inputs=[], run_id="r1", ledger_head=_head(run))
    assert full["probes"][0]["stdout"] == "gpu-model-x"


def test_the_printed_re_record_keeps_the_probes_and_clears_the_refusal(tmp_path, monkeypatch, capsys):
    """Refusal, run the printed command, the gate passes, and the new host entry still has the probe."""
    import gdkit.host as host
    from gdkit.run import main

    run, _ = _project_run(tmp_path, host=True)
    real = host._probe
    monkeypatch.setattr(host, "_probe", lambda command, timeout=30: {**real(command, timeout), "stdout": "gpu-y"})
    with pytest.raises(GateError, match=r"python -m gdkit\.run host (\S+) \(it reuses") as info:
        run_gate(run, COUNT, inputs=[], run_id="r1", ledger_head=_head(run))
    printed = str(info.value).split("re-record it with python -m gdkit.run host ", 1)[1].split(" (", 1)[0]
    assert main(["host", printed]) == 0
    capsys.readouterr()
    entries = [json.loads(line) for line in (run / "ledger.jsonl").read_text(encoding="utf-8").splitlines()]
    hosts = [e for e in entries if e["op"] == "host"]
    assert len(hosts) == 2 and len(hosts[-1]["params"]["summary"]["probes"]) == 1
    run_gate(run, COUNT, inputs=[], run_id="r1", ledger_head=_head(run))


def test_a_missing_host_json_means_another_machine(tmp_path):
    run, _ = _project_run(tmp_path, host=True)
    (run / "host.json").unlink()
    with pytest.raises(GateError, match="host.json is missing on this machine"):
        run_gate(run, COUNT, inputs=[], run_id="r1", ledger_head=_head(run))


def test_opting_out_is_visible_in_the_receipt(tmp_path):
    run, _ = _project_run(tmp_path, host=True)
    (run / "host.json").unlink()
    path = run_gate(run, COUNT, inputs=[], run_id="r1", ledger_head=_head(run), check_host=False)
    assert json.loads(path.read_text(encoding="utf-8"))["host_check"] == {
        "checked": False, "reason": "disabled by the caller (check_host=False)"}


def test_promote_and_attest_do_not_check_the_host(tmp_path):
    """CI promotes on another machine, without host.json."""
    run, project = _project_run(tmp_path, host=True)
    cheap = Gate("count", cheap=True, check=lambda run_dir: {"passed": True})
    costly = Gate("train", cheap=False, check=lambda run_dir: {"passed": True})
    for gate in (cheap, costly):
        run_gate(run, gate, inputs=[], run_id="r1", ledger_head=_head(run))
    (run / "host.json").unlink()
    registry = {"count": cheap, "train": costly}
    promote(run, registry, ["count", "train"], attestation=ACCEPT, project_path=project)
    attestable_hash(run, "train", registry, project_path=project)
