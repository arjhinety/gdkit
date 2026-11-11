import json
import sys
from pathlib import Path

import pytest

from gates_fixture import COUNT, TRAIN
from gdkit.cards import CardError, audit_card, dataset_card, model_card, write_card
from gdkit.gates import run_gate
from gdkit.ledger import Ledger
from gdkit.pwc_lookup import lookup

FAKE = [sys.executable, str(Path(__file__).parent / "fake_pwc.py")]
H = "a" * 64
SETUP = {
    "base_model_revision": "15852e8c",
    "tokenizer_sha256": "1" * 64,
    "config_sha256": "2" * 64,
    "eval_harness_sha256": "3" * 64,
    "prompt_template_sha256": "4" * 64,
}


def dataset_run(tmp_path):
    """10,000 raw rows, 500 rejected by validation, 2,000 duplicates removed, one column dropped."""
    run = tmp_path / "run"
    run.mkdir()
    ledger = Ledger(run / "ledger.jsonl", run_id="run-1", mode="registered")
    ledger.record("load", rows_before=0, rows_after=10_000, input_hash=H, output_hash=H)
    ledger.record("validate", rows_before=10_000, rows_after=9_500, input_hash=H, output_hash=H)
    ledger.record("drop_column", rows_before=9_500, rows_after=9_500, columns_dropped=["noise"],
                  input_hash=H, output_hash=H)
    ledger.record("dedup", rows_before=9_500, rows_after=7_500, input_hash=H, output_hash=H)
    return run


def model_run(tmp_path, setup=SETUP):
    run = dataset_run(tmp_path)
    (run / "model_setup.json").write_text(json.dumps(setup), encoding="utf-8")
    head = json.loads((run / "ledger.jsonl").read_text(encoding="utf-8").splitlines()[-1])["entry_hash"]
    data = run / "data.csv"
    data.write_text("a\nb\nc\n", encoding="utf-8")
    run_gate(run, COUNT, inputs=["data.csv"], run_id="run-1", ledger_head=head)
    run_gate(run, TRAIN, inputs=["data.csv"], run_id="run-1", ledger_head=head)
    return run


def test_dataset_counts_match_the_ledger(tmp_path):
    card = dataset_card(dataset_run(tmp_path))
    assert card["raw_rows"] == 10_000
    assert card["rejected_rows"] == 500
    assert card["valid_rows"] == 9_500
    assert card["deduplicated_rows"] == 2_000
    assert card["final_rows"] == 7_500
    assert card["columns_dropped"] == ["noise"]
    assert card["raw_rows"] - card["rejected_rows"] == card["valid_rows"]


def test_dataset_card_names_the_ledger_head(tmp_path):
    run = dataset_run(tmp_path)
    card = dataset_card(run)
    assert card["ledger_head"] == json.loads((run / "ledger.jsonl").read_text().splitlines()[-1])["entry_hash"]


def test_model_card_takes_its_fields_from_setup_receipts_and_lookups(tmp_path):
    run = model_run(tmp_path)
    lookup(Ledger(run / "ledger.jsonl", run_id="run-1", mode="registered"), run, ["search", "x"], pwc_bin=FAKE)
    card = model_card(run)
    for field, value in SETUP.items():
        assert card[field] == value
    assert set(card["gate_receipts"]) == {"row_count", "train"}
    assert card["paper_lookups_ok"] == 1 and card["paper_lookups_unavailable"] == 0


def test_model_card_refuses_a_missing_setup_field(tmp_path):
    setup = dict(SETUP)
    del setup["tokenizer_sha256"]
    with pytest.raises(CardError, match="tokenizer_sha256"):
        model_card(model_run(tmp_path, setup=setup))


def test_unchanged_card_passes_the_audit(tmp_path):
    run = dataset_run(tmp_path)
    card_path = write_card(run / "cards" / "dataset.json", dataset_card(run))
    assert audit_card(card_path, dataset_card(run)) == []


def test_hand_edited_count_fails_the_audit(tmp_path):
    run = dataset_run(tmp_path)
    card_path = write_card(run / "cards" / "dataset.json", dataset_card(run))
    edited = json.loads(card_path.read_text(encoding="utf-8"))
    edited["final_rows"] = 8_000
    card_path.write_text(json.dumps(edited), encoding="utf-8")
    assert audit_card(card_path, dataset_card(run)) == ["final_rows"]


def test_card_built_from_an_older_ledger_is_stale(tmp_path):
    run = dataset_run(tmp_path)
    card_path = write_card(run / "cards" / "dataset.json", dataset_card(run))
    Ledger(run / "ledger.jsonl", run_id="run-1", mode="registered").record(
        "split_train", rows_before=7_500, rows_after=6_000, input_hash=H, output_hash=H
    )
    stale = audit_card(card_path, dataset_card(run))
    assert "ledger_head" in stale and "final_rows" in stale


def test_dataset_counts_skip_the_preregistration_entry(tmp_path):
    """1000 loaded, 50 rejected: the card must say 1000 raw and 950 valid, not 0 and -50."""
    from gdkit.prereg import freeze_run

    run = tmp_path / "run"
    run.mkdir()
    project = tmp_path / "project.yaml"
    project.write_text("study_question: q\n", encoding="utf-8")
    ledger = Ledger(run / "ledger.jsonl", run_id="run-1", mode="registered")
    freeze_run(ledger, project)
    ledger.record("load", rows_before=0, rows_after=1000, input_hash=H, output_hash=H)
    ledger.record("validate", rows_before=1000, rows_after=950, input_hash=H, output_hash=H)
    card = dataset_card(run)
    assert (card["raw_rows"], card["rejected_rows"], card["valid_rows"], card["final_rows"]) == (1000, 50, 950, 950)


def test_the_model_card_shows_answers_that_replaced_an_explore_unset(tmp_path):
    from gdkit.prereg import freeze_run

    run = tmp_path / "run"
    run.mkdir()
    project = tmp_path / "project.yaml"
    project.write_text("study_question: q\nreplaced_unset: [study_question]\n", encoding="utf-8")
    freeze_run(Ledger(run / "ledger.jsonl", run_id="run-1", mode="registered"), project)
    (run / "model_setup.json").write_text(json.dumps(SETUP), encoding="utf-8")
    assert model_card(run)["preregistration_replaced_unset"] == ["study_question"]
    # A run with no preregistration entry says so instead of claiming an empty list.
    other = tmp_path / "other"
    other.mkdir()
    assert model_card(model_run(other))["preregistration_replaced_unset"] is None
