import hashlib
import json
import sys
from pathlib import Path

import pytest

from gdkit.ledger import Ledger, verify
from gdkit.pwc_lookup import lookup

FAKE = [sys.executable, str(Path(__file__).parent / "fake_pwc.py")]
H = "a" * 64


def _ledger(tmp_path, mode="registered"):
    run = tmp_path / "run"
    run.mkdir()
    return run, Ledger(run / "ledger.jsonl", run_id="run-1", mode=mode)


def _entries(run):
    return [json.loads(line) for line in (run / "ledger.jsonl").read_text(encoding="utf-8").splitlines()]


def test_successful_lookup_is_recorded_with_stable_and_volatile_fields(tmp_path):
    run, ledger = _ledger(tmp_path)
    params = lookup(ledger, run, ["search", "small vision language models", "--limit", "1"], pwc_bin=FAKE)
    assert params["status"] == "OK"
    assert params["pwc_version"] == "0.4.2-test" and params["api_version"] == "v1"
    assert params["schema_version"] == "v1"
    assert params["papers"] == [
        {"id": "26170", "arxiv_id": "2401.12503",
         "title": "Small Language Model Meets with Reinforced Vision Vocabulary", "published": "2024-01-23"}
    ]
    assert params["volatile"] == {
        "26170": {"citation_count": 52, "has_official_implementation": False, "code_repository_count": 0}
    }
    assert verify(run / "ledger.jsonl") == 1


def test_raw_response_is_kept_and_its_hash_is_recorded(tmp_path):
    run, ledger = _ledger(tmp_path)
    params = lookup(ledger, run, ["search", "x"], pwc_bin=FAKE)
    raw_file = run / params["raw_response_path"]
    assert raw_file.exists()
    assert hashlib.sha256(raw_file.read_bytes()).hexdigest() == params["response_sha256"]


def test_missing_binary_writes_an_unavailable_entry(tmp_path):
    run, ledger = _ledger(tmp_path)
    params = lookup(ledger, run, ["search", "x"], pwc_bin=["/no/such/pwc-binary"])
    assert params["status"] == "UNAVAILABLE"
    assert "did not run" in params["reason"]
    assert verify(run / "ledger.jsonl") == 1
    assert not (run / "paper_lookups").exists()


def test_nonzero_exit_writes_an_unavailable_entry_with_the_reason(tmp_path):
    run, ledger = _ledger(tmp_path)
    params = lookup(ledger, run, ["fail", "x"], pwc_bin=FAKE)
    assert params["status"] == "UNAVAILABLE"
    assert "status 2" in params["reason"] and "network error" in params["reason"]


def test_non_json_response_is_unavailable_and_keeps_no_raw_file(tmp_path):
    run, ledger = _ledger(tmp_path)
    params = lookup(ledger, run, ["garbage", "x"], pwc_bin=FAKE)
    assert params["status"] == "UNAVAILABLE"
    assert "not JSON" in params["reason"]


def test_registered_mode_puts_request_and_response_hashes_in_the_content_fields(tmp_path):
    run, ledger = _ledger(tmp_path, mode="registered")
    lookup(ledger, run, ["search", "x"], pwc_bin=FAKE)
    entry = _entries(run)[0]
    assert entry["input_hash"] == entry["params"]["request_sha256"]
    assert entry["output_hash"] == entry["params"]["response_sha256"]


def test_explore_mode_records_the_hashes_in_params_only(tmp_path):
    run, ledger = _ledger(tmp_path, mode="explore")
    lookup(ledger, run, ["search", "x"], pwc_bin=FAKE)
    entry = _entries(run)[0]
    assert entry["input_hash"] is None and entry["output_hash"] is None
    assert entry["params"]["response_sha256"]
    assert verify(run / "ledger.jsonl") == 1


@pytest.mark.parametrize("mode", ["jsonlist", "badresults"])
def test_json_that_is_not_the_expected_shape_is_unavailable_and_recorded(tmp_path, mode):
    run, ledger = _ledger(tmp_path)
    params = lookup(ledger, run, [mode, "x"], pwc_bin=FAKE)
    assert params["status"] == "UNAVAILABLE"
    assert "shape" in params["reason"]
    assert verify(run / "ledger.jsonl") == 1
    assert not (run / "paper_lookups").exists()


def test_a_stale_second_writer_does_not_overwrite_a_recorded_raw_file(tmp_path):
    import pytest as _pytest

    from gdkit.ledger import LedgerError

    run, first = _ledger(tmp_path)
    second = Ledger(run / "ledger.jsonl", run_id="run-1", mode="registered")
    recorded = lookup(first, run, ["search", "a"], pwc_bin=FAKE)
    before = (run / recorded["raw_response_path"]).read_bytes()
    with _pytest.raises(LedgerError, match="changed on disk"):
        lookup(second, run, ["search", "b"], pwc_bin=FAKE)
    assert (run / recorded["raw_response_path"]).read_bytes() == before
    assert len(list((run / "paper_lookups").iterdir())) == 1


def test_raw_files_are_named_by_ledger_position_so_a_deleted_file_is_not_overwritten(tmp_path):
    run, ledger = _ledger(tmp_path)
    first = lookup(ledger, run, ["search", "a"], pwc_bin=FAKE)
    second = lookup(ledger, run, ["search", "b"], pwc_bin=FAKE)
    (run / first["raw_response_path"]).unlink()
    third = lookup(ledger, run, ["search", "c"], pwc_bin=FAKE)
    assert len({first["raw_response_path"], second["raw_response_path"], third["raw_response_path"]}) == 3
    assert (run / second["raw_response_path"]).exists()
