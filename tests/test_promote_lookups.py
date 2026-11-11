import hashlib
import sys
from pathlib import Path

import pytest

from gates_fixture import COUNT, REGISTRY, TRAIN
from gdkit.gates import run_gate
from gdkit.ledger import Ledger
from gdkit.promote import PromoteError, attestable_hash, promote
from prereg_fixture import freeze, proj
from gdkit.pwc_lookup import lookup

FAKE = [sys.executable, str(Path(__file__).parent / "fake_pwc.py")]
RUN_ID = "run-1"
REQUIRED = ["row_count", "train"]
ACCEPT = lambda run_id, gate, digest: True  # noqa: E731


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def build(tmp_path, lookup_calls):
    """A registered run with the given paper lookups, then receipts for both gates.

    Lookups come before the receipts, because each receipt binds the ledger head at the time it is written.
    """
    run = tmp_path / "run"
    run.mkdir()
    data = run / "data.csv"
    data.write_text("a\nb\nc\n", encoding="utf-8")
    ledger = Ledger(run / "ledger.jsonl", run_id=RUN_ID, mode="registered")
    freeze(ledger, run)
    ledger.record("load", rows_before=0, rows_after=3, input_hash=_sha(data), output_hash=_sha(data))
    for args in lookup_calls:
        lookup(ledger, run, args, pwc_bin=FAKE)
    head = ledger.path.read_text(encoding="utf-8").splitlines()[-1]
    import json

    head = json.loads(head)["entry_hash"]
    run_gate(run, COUNT, inputs=["data.csv"], run_id=RUN_ID, ledger_head=head)
    run_gate(run, TRAIN, inputs=["data.csv"], run_id=RUN_ID, ledger_head=head)
    return run


def test_ok_lookup_does_not_block(tmp_path):
    run = build(tmp_path, [["search", "x"]])
    assert promote(run, REGISTRY, REQUIRED, attestation=ACCEPT, project_path=proj(run))["gates"]


def test_unavailable_lookup_blocks_promote(tmp_path):
    run = build(tmp_path, [["fail", "x"]])
    with pytest.raises(PromoteError, match="still UNAVAILABLE"):
        promote(run, REGISTRY, REQUIRED, attestation=ACCEPT, project_path=proj(run))


def test_unavailable_lookup_also_blocks_attestation(tmp_path):
    run = build(tmp_path, [["fail", "x"]])
    with pytest.raises(PromoteError, match="still UNAVAILABLE"):
        attestable_hash(run, "train", REGISTRY, project_path=proj(run))


def test_successful_retry_of_the_same_request_clears_the_block(tmp_path, monkeypatch):
    marker = tmp_path / "outage.marker"
    monkeypatch.setenv("FAKE_PWC_MARKER", str(marker))
    run = tmp_path / "run"
    run.mkdir()
    data = run / "data.csv"
    data.write_text("a\nb\nc\n", encoding="utf-8")
    ledger = Ledger(run / "ledger.jsonl", run_id=RUN_ID, mode="registered")
    freeze(ledger, run)
    ledger.record("load", rows_before=0, rows_after=3, input_hash=_sha(data), output_hash=_sha(data))
    first = lookup(ledger, run, ["flaky", "x"], pwc_bin=FAKE)
    assert first["status"] == "UNAVAILABLE"
    second = lookup(ledger, run, ["flaky", "x"], pwc_bin=FAKE)
    assert second["status"] == "OK"
    assert second["request_sha256"] == first["request_sha256"]

    import json

    head = json.loads((run / "ledger.jsonl").read_text(encoding="utf-8").splitlines()[-1])["entry_hash"]
    run_gate(run, COUNT, inputs=["data.csv"], run_id=RUN_ID, ledger_head=head)
    run_gate(run, TRAIN, inputs=["data.csv"], run_id=RUN_ID, ledger_head=head)
    assert promote(run, REGISTRY, REQUIRED, attestation=ACCEPT, project_path=proj(run))["gates"]


def test_a_failure_after_an_earlier_success_blocks_again(tmp_path, monkeypatch):
    marker = tmp_path / "outage.marker"
    monkeypatch.setenv("FAKE_PWC_MARKER", str(marker))
    run = tmp_path / "run"
    run.mkdir()
    data = run / "data.csv"
    data.write_text("a\nb\nc\n", encoding="utf-8")
    ledger = Ledger(run / "ledger.jsonl", run_id=RUN_ID, mode="registered")
    freeze(ledger, run)
    ledger.record("load", rows_before=0, rows_after=3, input_hash=_sha(data), output_hash=_sha(data))
    lookup(ledger, run, ["flaky", "x"], pwc_bin=FAKE)  # fails, writes the marker
    assert lookup(ledger, run, ["flaky", "x"], pwc_bin=FAKE)["status"] == "OK"
    marker.unlink()  # next call fails again
    assert lookup(ledger, run, ["flaky", "x"], pwc_bin=FAKE)["status"] == "UNAVAILABLE"

    import json

    head = json.loads((run / "ledger.jsonl").read_text(encoding="utf-8").splitlines()[-1])["entry_hash"]
    run_gate(run, COUNT, inputs=["data.csv"], run_id=RUN_ID, ledger_head=head)
    run_gate(run, TRAIN, inputs=["data.csv"], run_id=RUN_ID, ledger_head=head)
    with pytest.raises(PromoteError, match="still UNAVAILABLE"):
        promote(run, REGISTRY, REQUIRED, attestation=ACCEPT, project_path=proj(run))
