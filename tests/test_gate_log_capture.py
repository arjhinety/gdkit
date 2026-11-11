"""File-descriptor-level gate log capture, tested in a real child process.

Under pytest, sys.stdout and sys.stderr are pytest's capture objects, not descriptors 1 and 2, so a logging handler
created inside a test would never write through descriptor 2. Each case therefore runs a small script in a fresh
Python process, where the streams are the real ones.
"""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

SCRIPT = r'''
import json, logging, subprocess, sys
from pathlib import Path
from gdkit.gates import Gate, run_gate

mode, run = sys.argv[1], Path(sys.argv[2])

handler = logging.StreamHandler()  # bound to the real sys.stderr now, before run_gate
logger = logging.getLogger("gate-test")
logger.addHandler(handler)
logger.setLevel(logging.INFO)


def via_logging(run_dir):
    logger.info("line from a logging handler made before run_gate")
    return {"passed": True}


def via_subprocess(run_dir):
    subprocess.run([sys.executable, "-c", "print('line from a child process')"], check=True)
    return {"passed": True}


check = via_logging if mode == "logging" else via_subprocess
gate = Gate("fdgate", cheap=True, check=check)

if mode == "promote":
    from gdkit.ledger import Ledger
    from gdkit.prereg import freeze_run
    from gdkit.promote import promote

    project = run.parent / "project.yaml"
    project.write_text("study_question: q\n", encoding="utf-8")
    entry = freeze_run(Ledger(run / "ledger.jsonl", run_id="r", mode="registered"), project)
    run_gate(run, gate, inputs=[], run_id="r", ledger_head=entry["entry_hash"], capture_log=True)
    before = (run / "logs" / "fdgate.log").read_bytes()
    for _ in range(2):
        promote(run, {"fdgate": gate}, ["fdgate"], project_path=project)
    assert (run / "logs" / "fdgate.log").read_bytes() == before, "promote rewrote the log"
else:
    run_gate(run, gate, inputs=[], run_id="r", ledger_head="0" * 64, capture_log=True)

print("stdout is back after run_gate")
logger.info("stderr is back after run_gate")
'''


def _run(tmp_path, mode):
    script = tmp_path / "gate_script.py"
    script.write_text(SCRIPT, encoding="utf-8")
    run = tmp_path / "run"
    run.mkdir()
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(ROOT), os.environ.get("PYTHONPATH", "")])}
    proc = subprocess.run([sys.executable, str(script), mode, str(run)], capture_output=True, env=env, cwd=tmp_path)
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    return run, proc


def _log_and_receipt(run):
    log = (run / "logs" / "fdgate.log").read_bytes()
    receipt = json.loads((run / "receipts" / "fdgate.json").read_text(encoding="utf-8"))
    return log, receipt


@pytest.mark.parametrize(
    "mode, line",
    [("logging", b"line from a logging handler made before run_gate"), ("subprocess", b"line from a child process")],
)
def test_output_written_below_python_reaches_the_log_and_its_hash(tmp_path, mode, line):
    run, proc = _run(tmp_path, mode)
    log, receipt = _log_and_receipt(run)
    assert line in log
    assert receipt["log"]["sha256"] == hashlib.sha256(log).hexdigest()
    assert receipt["log"]["bytes"] == len(log)
    # The capture ends with the check: later output goes to the real streams, and none of the check's leaks out.
    assert b"stdout is back after run_gate" in proc.stdout and b"stdout is back" not in log
    assert b"stderr is back after run_gate" in proc.stderr and b"stderr is back" not in log
    assert line not in proc.stdout and line not in proc.stderr


def test_the_partial_log_exists_and_grows_while_the_check_runs(tmp_path):
    from gdkit.gates import Gate, run_gate

    partial = tmp_path / "logs" / "watched.log.partial"
    sizes = []

    def check(run_dir):
        for line in ("first line", "second line", "third line"):
            print(line)  # line-buffered, so each line reaches the file before the next size is read
            sizes.append(partial.stat().st_size if partial.exists() else None)
        return {"passed": True}

    receipt_path = run_gate(tmp_path, Gate("watched", cheap=True, check=check), inputs=[], run_id="r",
                            ledger_head="0" * 64, capture_log=True)
    assert None not in sizes and sizes[0] < sizes[1] < sizes[2]
    assert not partial.exists()
    final = tmp_path / "logs" / "watched.log"
    assert final.read_bytes() == b"first line\nsecond line\nthird line\n"
    log = json.loads(receipt_path.read_text(encoding="utf-8"))["log"]
    assert log["sha256"] == hashlib.sha256(final.read_bytes()).hexdigest()
    assert log["platform"] == sys.platform and log["locale_encoding"] and log["gdkit_stream_encoding"] == "utf-8"


def test_a_blocked_rename_falls_back_to_a_copy(tmp_path, monkeypatch):
    """Portable: the rename is made to fail the way Windows fails it while a reader holds the file."""
    import gdkit.gates as gates

    def refuse(src, dst):
        raise PermissionError(32, "The process cannot access the file because it is being used by another process")

    monkeypatch.setattr(gates.os, "replace", refuse)

    def check(run_dir):
        print("copied, not renamed")
        return {"passed": True}

    receipt_path = gates.run_gate(tmp_path, gates.Gate("held", cheap=True, check=check), inputs=[], run_id="r",
                                  ledger_head="0" * 64, capture_log=True)
    final = tmp_path / "logs" / "held.log"
    log = json.loads(receipt_path.read_text(encoding="utf-8"))["log"]
    assert final.read_bytes() == b"copied, not renamed\n"
    assert log["sha256"] == hashlib.sha256(final.read_bytes()).hexdigest()
    assert log["finalised"] == "copied" and log["partial_left"] is False
    assert not (tmp_path / "logs" / "held.log.partial").exists()


def test_an_unblocked_rename_is_recorded_as_renamed(tmp_path):
    from gdkit.gates import Gate, run_gate

    receipt_path = run_gate(tmp_path, Gate("plain", cheap=True, check=lambda run_dir: {"passed": True}), inputs=[],
                            run_id="r", ledger_head="0" * 64, capture_log=True)
    log = json.loads(receipt_path.read_text(encoding="utf-8"))["log"]
    assert log["finalised"] == "renamed" and log["partial_left"] is False


@pytest.mark.skipif(sys.platform != "win32", reason="only Windows blocks a rename while a reader holds the file")
def test_a_real_reader_on_windows_still_gets_a_receipt(tmp_path):
    """A second process holds the partial log open with default sharing while the gate finishes."""
    from gdkit.gates import Gate, run_gate

    partial = tmp_path / "logs" / "watched.log.partial"
    readers = []

    def check(run_dir):
        print("written while watched")
        readers.append(subprocess.Popen(
            [sys.executable, "-c", f"import time; f = open(r'{partial}', 'rb'); print('open', flush=True); "
                                   "time.sleep(30)"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL))
        assert readers[0].stdout.readline().strip() == b"open"  # the reader holds the file from here
        return {"passed": True}

    try:
        receipt_path = run_gate(tmp_path, Gate("watched", cheap=True, check=check), inputs=[], run_id="r",
                                ledger_head="0" * 64, capture_log=True)
        log = json.loads(receipt_path.read_text(encoding="utf-8"))["log"]
        final = tmp_path / "logs" / "watched.log"
        assert final.read_bytes() == b"written while watched\n"
        assert log["sha256"] == hashlib.sha256(final.read_bytes()).hexdigest()
        assert log["finalised"] == "copied" and log["partial_left"] is True  # the reader still holds it
    finally:
        for reader in readers:
            reader.kill()
            reader.wait()


def test_a_check_that_raises_still_renames_its_log(tmp_path):
    from gdkit.gates import Gate, run_gate

    def check(run_dir):
        print("about to fail")
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        run_gate(tmp_path, Gate("fails", cheap=True, check=check), inputs=[], run_id="r",
                 ledger_head="0" * 64, capture_log=True)
    assert (tmp_path / "logs" / "fails.log").read_bytes() == b"about to fail\n"
    assert not (tmp_path / "logs" / "fails.log.partial").exists()
    assert not (tmp_path / "receipts" / "fails.json").exists()


def test_promote_re_runs_never_write_the_log_under_fd_capture(tmp_path):
    run, proc = _run(tmp_path, "promote")
    log, _ = _log_and_receipt(run)
    assert b"line from a child process" in log
    # The two re-runs printed to the real stdout instead.
    assert proc.stdout.count(b"line from a child process") == 2
