import hashlib
import json

import pytest

from gdkit.ledger import Ledger, LedgerError, verify

H1 = "a" * 64
H2 = "b" * 64


def _explore_scenario(path):
    """10,000 rows: load (boundary), drop a column, dedup 2,000 rows, split 80/20, write train (boundary)."""
    ledger = Ledger(path, run_id="run-1", mode="explore")
    ledger.record("load_raw", rows_before=0, rows_after=10_000, boundary=True, input_hash=H1, output_hash=H1)
    ledger.record("drop_column", rows_before=10_000, rows_after=10_000, columns_dropped=["noise"])
    ledger.record("dedup", rows_before=10_000, rows_after=8_000, params={"keys": ["text"]})
    ledger.record("split_train", rows_before=8_000, rows_after=6_400, boundary=True, input_hash=H1, output_hash=H2)
    return ledger


def test_explore_scenario_records_each_step_and_verifies(tmp_path):
    path = tmp_path / "ledger.jsonl"
    _explore_scenario(path)
    assert verify(path) == 4
    entries = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [e["op"] for e in entries] == ["load_raw", "drop_column", "dedup", "split_train"]
    assert entries[1]["rows_before"] == entries[1]["rows_after"] == 10_000
    assert entries[1]["columns_dropped"] == ["noise"]
    assert entries[2]["rows_before"] == 10_000 and entries[2]["rows_after"] == 8_000
    assert entries[3]["rows_after"] == 6_400
    # Intermediate entries carry no content hashes in explore mode.
    assert entries[1]["input_hash"] is None and entries[2]["output_hash"] is None
    # Boundary entries carry both hashes.
    assert entries[0]["output_hash"] == H1 and entries[3]["output_hash"] == H2


def test_explore_rejects_hashes_on_intermediate_entries(tmp_path):
    ledger = Ledger(tmp_path / "l.jsonl", run_id="r", mode="explore")
    with pytest.raises(LedgerError):
        ledger.record("dedup", rows_before=10, rows_after=8, input_hash=H1, output_hash=H2)


def test_explore_boundary_needs_both_hashes(tmp_path):
    ledger = Ledger(tmp_path / "l.jsonl", run_id="r", mode="explore")
    with pytest.raises(LedgerError):
        ledger.record("load_raw", rows_before=0, rows_after=10, boundary=True, input_hash=H1)


def test_registered_requires_both_hashes_on_every_entry(tmp_path):
    ledger = Ledger(tmp_path / "l.jsonl", run_id="r", mode="registered")
    with pytest.raises(LedgerError):
        ledger.record("drop_column", rows_before=10, rows_after=10, columns_dropped=["c"])
    entry = ledger.record(
        "drop_column", rows_before=10, rows_after=10, columns_dropped=["c"], input_hash=H1, output_hash=H2
    )
    assert entry["input_hash"] == H1 and entry["output_hash"] == H2


def test_unknown_mode_is_rejected(tmp_path):
    with pytest.raises(LedgerError):
        Ledger(tmp_path / "l.jsonl", run_id="r", mode="guess")


def test_editing_a_line_breaks_verification(tmp_path):
    path = tmp_path / "ledger.jsonl"
    _explore_scenario(path)
    lines = path.read_text(encoding="utf-8").splitlines()
    entry = json.loads(lines[1])
    entry["rows_after"] = 9_999
    lines[1] = json.dumps(entry, sort_keys=True, separators=(",", ":"))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(LedgerError, match="line 2"):
        verify(path)


def test_reopening_continues_the_chain(tmp_path):
    path = tmp_path / "ledger.jsonl"
    _explore_scenario(path)
    reopened = Ledger(path, run_id="run-1", mode="explore")
    reopened.record("late_step", rows_before=6_400, rows_after=6_400)
    assert verify(path) == 5


def test_file_hash_helper_matches_hashlib(tmp_path):
    from opengrad.hashing import sha256_file

    sample = tmp_path / "raw.bin"
    sample.write_bytes(b"gdkit" * 10_000)
    assert sha256_file(sample) == hashlib.sha256(sample.read_bytes()).hexdigest()
