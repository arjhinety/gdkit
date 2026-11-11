"""Answers decided after an explore run: skips are recorded, changes are declared, and the registered freeze
refuses any difference from an earlier explore freeze that the project file does not declare."""

from pathlib import Path

import pytest
import yaml

from gdkit.clinic import run_interview
from gdkit.interview import (
    CHANGED_AFTER_EXPLORE_KEY,
    REPLACED_UNSET_KEY,
    SKIPPED_IN_EXPLORE_KEY,
    UNSET,
    load_questions,
    validate,
)
from gdkit.ledger import Ledger
from gdkit.prereg import PreregError, check_answers, freeze_run
from gdkit.skill_api import change_answer, declare_answer, main, next_question, set_answer

ROOT = Path(__file__).resolve().parents[1]
QUESTIONS_PATH = ROOT / "interview" / "questions.yaml"
QUESTIONS = load_questions(QUESTIONS_PATH)


def _write(path: Path, data: dict) -> Path:
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _freeze(runs: Path, run_id: str, mode: str, project: Path) -> dict:
    run = runs / run_id
    run.mkdir(parents=True, exist_ok=True)  # a refused freeze leaves the directory, and the retry reuses it
    return freeze_run(Ledger(run / "ledger.jsonl", run_id=run_id, mode=mode), project)


# --- skips in explore mode -------------------------------------------------------------------


def test_an_explore_skip_is_recorded_and_not_asked_again_in_explore(tmp_path):
    project = tmp_path / "project.yaml"
    ok, message = set_answer(QUESTIONS, project, "explore", "baseline", "")
    assert ok, message
    assert _load(project)[SKIPPED_IN_EXPLORE_KEY] == ["baseline"]
    answers = {q["id"]: "x" for q in QUESTIONS["questions"] if q["id"] != "baseline"}
    answers[SKIPPED_IN_EXPLORE_KEY] = ["baseline"]
    assert next_question(QUESTIONS, answers, "explore")["done"] is True
    assert next_question(QUESTIONS, answers, "registered")["id"] == "baseline"


def test_a_skipped_question_can_still_be_answered_in_explore_mode(tmp_path):
    project = tmp_path / "project.yaml"
    set_answer(QUESTIONS, project, "explore", "baseline", "")
    ok, message = set_answer(QUESTIONS, project, "explore", "baseline", "parent at 0.70")
    assert ok, message
    saved = _load(project)
    assert saved["baseline"] == "parent at 0.70" and saved[REPLACED_UNSET_KEY] == ["baseline"]
    assert SKIPPED_IN_EXPLORE_KEY not in saved


def test_an_answer_given_after_an_explore_skip_joins_replaced_unset(tmp_path):
    project = tmp_path / "project.yaml"
    set_answer(QUESTIONS, project, "explore", "baseline", "")
    result = next_question(QUESTIONS, _load(project), "registered")
    assert result["id"] == "study_question"  # still asked in order; baseline comes later
    ok, message = set_answer(QUESTIONS, project, "registered", "baseline", "parent at 0.70")
    assert ok, message
    saved = _load(project)
    assert saved[REPLACED_UNSET_KEY] == ["baseline"]
    assert "baseline" not in saved.get(SKIPPED_IN_EXPLORE_KEY, [])


def test_a_registered_blank_is_still_refused_and_not_recorded_as_a_skip(tmp_path):
    project = tmp_path / "project.yaml"
    ok, _ = set_answer(QUESTIONS, project, "registered", "baseline", "")
    assert not ok
    assert not project.exists()


def test_the_clinic_records_explore_skips_and_their_later_answers(tmp_path):
    project = tmp_path / "project.yaml"
    blanks = [""] * len(QUESTIONS["questions"])
    run_interview(QUESTIONS_PATH, project, "explore", ask=lambda _: blanks.pop(0), out=lambda _: None)
    all_ids = [q["id"] for q in QUESTIONS["questions"]]
    assert _load(project)[SKIPPED_IN_EXPLORE_KEY] == all_ids
    # A second explore pass does not ask the skipped questions again.
    run_interview(QUESTIONS_PATH, project, "explore", ask=lambda _: pytest.fail("asked again"), out=lambda _: None)


# --- declared changes ------------------------------------------------------------------------


def test_change_replaces_a_real_answer_and_declares_it(tmp_path):
    project = _write(tmp_path / "project.yaml", {"refusal_threshold": 0.05})
    ok, message = change_answer(QUESTIONS, project, "registered", "refusal_threshold", "0.03")
    assert ok, message
    saved = _load(project)
    assert saved["refusal_threshold"] == 0.03
    assert saved[CHANGED_AFTER_EXPLORE_KEY] == ["refusal_threshold"]
    assert not any(p.startswith(CHANGED_AFTER_EXPLORE_KEY) for p in validate(saved, QUESTIONS, "explore"))


@pytest.mark.parametrize("existing", [{}, {"refusal_threshold": UNSET}])
def test_change_refuses_a_question_with_no_real_answer(tmp_path, existing):
    project = _write(tmp_path / "project.yaml", existing)
    ok, message = change_answer(QUESTIONS, project, "registered", "refusal_threshold", "0.03")
    assert not ok and "use set" in message


def test_change_refuses_an_invalid_value(tmp_path):
    project = _write(tmp_path / "project.yaml", {"refusal_threshold": 0.05})
    ok, message = change_answer(QUESTIONS, project, "registered", "refusal_threshold", "lots")
    assert not ok
    assert _load(project) == {"refusal_threshold": 0.05}


def test_change_is_on_the_command_line(tmp_path, capsys):
    project = _write(tmp_path / "project.yaml", {"refusal_threshold": 0.05})
    status = main(["change", "--project", str(project), "--questions", str(QUESTIONS_PATH), "--mode", "registered",
                   "--id", "refusal_threshold", "--value", "0.03"])
    assert status == 0, capsys.readouterr().out
    assert _load(project)[CHANGED_AFTER_EXPLORE_KEY] == ["refusal_threshold"]


@pytest.mark.parametrize("key", [SKIPPED_IN_EXPLORE_KEY, CHANGED_AFTER_EXPLORE_KEY])
def test_the_bookkeeping_lists_must_name_questions(key):
    assert not any(p.startswith(key) for p in validate({key: ["baseline"]}, QUESTIONS, "explore"))
    assert any(p.startswith(key) for p in validate({key: ["basline"]}, QUESTIONS, "explore"))


# --- the registered freeze checks earlier explore freezes ------------------------------------


def _explore_then_registered(tmp_path, explore_answers, registered_answers):
    runs = tmp_path / "runs"
    _freeze(runs, "explore-1", "explore", _write(tmp_path / "explore.yaml", explore_answers))
    return runs, _write(tmp_path / "project.yaml", registered_answers)


def test_an_undeclared_changed_answer_refuses_the_freeze(tmp_path):
    runs, project = _explore_then_registered(
        tmp_path, {"refusal_threshold": 0.05}, {"refusal_threshold": 0.03}
    )
    with pytest.raises(PreregError, match="refusal_threshold") as info:
        _freeze(runs, "reg-1", "registered", project)
    assert "explore-1" in str(info.value)
    assert not (runs / "reg-1" / "ledger.jsonl").exists()


def test_a_declared_change_is_frozen_and_recorded(tmp_path):
    runs, project = _explore_then_registered(
        tmp_path, {"refusal_threshold": 0.05},
        {"refusal_threshold": 0.03, CHANGED_AFTER_EXPLORE_KEY: ["refusal_threshold"]},
    )
    entry = _freeze(runs, "reg-1", "registered", project)
    assert entry["params"][CHANGED_AFTER_EXPLORE_KEY] == ["refusal_threshold"]
    assert entry["params"]["explore_runs_checked"] == ["explore-1"]
    assert CHANGED_AFTER_EXPLORE_KEY not in entry["params"]["answers"]


@pytest.mark.parametrize("explore_answers", [{"baseline": UNSET}, {}], ids=["unset", "skipped"])
def test_an_undeclared_answer_to_an_undecided_question_refuses_the_freeze(tmp_path, explore_answers):
    runs, project = _explore_then_registered(tmp_path, explore_answers, {"baseline": "parent at 0.70"})
    with pytest.raises(PreregError, match="baseline"):
        _freeze(runs, "reg-1", "registered", project)


def test_the_same_answer_declared_as_replaced_unset_is_frozen(tmp_path):
    runs, project = _explore_then_registered(
        tmp_path, {"baseline": UNSET}, {"baseline": "parent at 0.70", REPLACED_UNSET_KEY: ["baseline"]}
    )
    assert _freeze(runs, "reg-1", "registered", project)["params"][REPLACED_UNSET_KEY] == ["baseline"]


def test_an_answer_dropped_since_explore_must_be_declared(tmp_path):
    runs, project = _explore_then_registered(tmp_path, {"baseline": "b", "intended_use": "x"}, {"baseline": "b"})
    with pytest.raises(PreregError, match="intended_use"):
        _freeze(runs, "reg-1", "registered", project)


def test_unchanged_answers_need_no_declaration(tmp_path):
    runs, project = _explore_then_registered(tmp_path, {"baseline": "b"}, {"baseline": "b"})
    entry = _freeze(runs, "reg-1", "registered", project)
    assert entry["params"]["explore_runs_checked"] == ["explore-1"]


def test_an_explore_freeze_is_not_checked_against_earlier_explore_runs(tmp_path):
    runs, project = _explore_then_registered(tmp_path, {"baseline": "a"}, {"baseline": "b"})
    _freeze(runs, "explore-2", "explore", project)


def test_earlier_registered_runs_are_not_compared(tmp_path):
    runs = tmp_path / "runs"
    _freeze(runs, "reg-0", "registered", _write(tmp_path / "old.yaml", {"baseline": "a"}))
    _freeze(runs, "reg-1", "registered", _write(tmp_path / "project.yaml", {"baseline": "b"}))


def test_an_earlier_ledger_that_does_not_verify_refuses_the_freeze(tmp_path):
    runs, project = _explore_then_registered(tmp_path, {"baseline": "b"}, {"baseline": "b"})
    with open(runs / "explore-1" / "ledger.jsonl", "a", encoding="utf-8") as handle:
        handle.write('{"cut off')
    with pytest.raises(PreregError, match="explore-1"):
        _freeze(runs, "reg-1", "registered", project)


def test_the_model_card_shows_the_declarations_and_the_runs_checked(tmp_path):
    import json

    from gdkit.cards import model_card

    runs, project = _explore_then_registered(
        tmp_path, {"refusal_threshold": 0.05, "baseline": UNSET},
        {"refusal_threshold": 0.03, "baseline": "b",
         CHANGED_AFTER_EXPLORE_KEY: ["refusal_threshold"], REPLACED_UNSET_KEY: ["baseline"]},
    )
    _freeze(runs, "reg-1", "registered", project)
    setup = {"base_model_revision": "r", "tokenizer_sha256": "1" * 64, "config_sha256": "2" * 64,
             "eval_harness_sha256": "3" * 64, "prompt_template_sha256": "4" * 64}
    (runs / "reg-1" / "model_setup.json").write_text(json.dumps(setup), encoding="utf-8")
    card = model_card(runs / "reg-1")
    assert card["preregistration_changed_after_explore"] == ["refusal_threshold"]
    assert card["preregistration_replaced_unset"] == ["baseline"]
    assert card["preregistration_explore_runs_checked"] == ["explore-1"]


def test_a_damaged_registered_sibling_does_not_block_the_freeze(tmp_path):
    runs = tmp_path / "runs"
    _freeze(runs, "reg-0", "registered", _write(tmp_path / "old.yaml", {"baseline": "a"}))
    with open(runs / "reg-0" / "ledger.jsonl", "a", encoding="utf-8") as handle:
        handle.write('{"cut off')
    (runs / "blank").mkdir()
    (runs / "blank" / "ledger.jsonl").write_text("\n  \n", encoding="utf-8")
    _freeze(runs, "reg-1", "registered", _write(tmp_path / "project.yaml", {"baseline": "b"}))


def test_an_unreadable_first_entry_blocks_the_freeze(tmp_path):
    runs = tmp_path / "runs"
    (runs / "mystery").mkdir(parents=True)
    (runs / "mystery" / "ledger.jsonl").write_text('{"op": "preregistration", "mode": "regis', encoding="utf-8")
    with pytest.raises(PreregError, match="mystery"):
        _freeze(runs, "reg-1", "registered", _write(tmp_path / "project.yaml", {"baseline": "b"}))


def test_end_to_end_with_tools_only_a_never_asked_question_can_be_declared(tmp_path):
    """Explore interview stopped after one answer, explore freeze, registered completion: no hand edits."""
    runs = tmp_path / "runs"
    project = tmp_path / "project.yaml"
    assert set_answer(QUESTIONS, project, "explore", "study_question", "Is it better?")[0]
    _freeze(runs, "explore-1", "explore", project)
    assert set_answer(QUESTIONS, project, "registered", "baseline", "parent at 0.70")[0]
    assert change_answer(QUESTIONS, project, "registered", "study_question", "Is it better on slice A?")[0]

    with pytest.raises(PreregError) as info:
        _freeze(runs, "reg-1", "registered", project)
    assert "skill_api declare --id baseline --list replaced_unset" in str(info.value)

    status = main(["declare", "--project", str(project), "--questions", str(QUESTIONS_PATH),
                   "--id", "baseline", "--list", "replaced_unset"])
    assert status == 0
    entry = _freeze(runs, "reg-1", "registered", project)
    assert entry["params"][REPLACED_UNSET_KEY] == ["baseline"]
    assert entry["params"][CHANGED_AFTER_EXPLORE_KEY] == ["study_question"]


def test_a_dropped_answer_is_declared_as_a_change(tmp_path):
    runs, project = _explore_then_registered(tmp_path, {"baseline": "b", "intended_use": "x"}, {"baseline": "b"})
    with pytest.raises(PreregError, match="declare --id intended_use --list changed_after_explore"):
        _freeze(runs, "reg-1", "registered", project)
    ok, message = declare_answer(QUESTIONS, project, "intended_use", CHANGED_AFTER_EXPLORE_KEY)
    assert ok, message
    _freeze(runs, "reg-1", "registered", project)


def test_declare_refuses_unknown_questions_and_lists(tmp_path):
    project = _write(tmp_path / "project.yaml", {"baseline": "b"})
    assert not declare_answer(QUESTIONS, project, "basline", REPLACED_UNSET_KEY)[0]
    assert not declare_answer(QUESTIONS, project, "baseline", SKIPPED_IN_EXPLORE_KEY)[0]
    assert declare_answer(QUESTIONS, project, "baseline", REPLACED_UNSET_KEY)[0]
    assert declare_answer(QUESTIONS, project, "baseline", REPLACED_UNSET_KEY)[0]  # repeating it is harmless
    assert _load(project)[REPLACED_UNSET_KEY] == ["baseline"]


def test_the_freeze_saves_the_folder_it_searched_relative_to_the_project_root(tmp_path):
    runs, project = _explore_then_registered(tmp_path, {"baseline": "b"}, {"baseline": "b"})
    entry = _freeze(runs, "reg-1", "registered", project)
    assert entry["params"]["explore_runs_dir"] == "runs"


def test_a_ledger_outside_a_runs_folder_records_where_the_empty_search_looked(tmp_path):
    """The project keeps its explore run under runs/, but this run's ledger sits in work/reg-1/. The default
    search looks next to the run's own folder, in work/, finds no explore freeze, and freezes. The saved folder
    and the empty list show a reviewer that nothing was compared."""
    _freeze(tmp_path / "runs", "explore-1", "explore", _write(tmp_path / "explore.yaml", {"baseline": "a"}))
    gdkit_dir = tmp_path / ".gdkit"
    gdkit_dir.mkdir()
    project = _write(gdkit_dir / "project.yaml", {"baseline": "b"})  # differs from explore-1, undeclared
    entry = _freeze(tmp_path / "work", "reg-1", "registered", project)
    assert entry["params"]["explore_runs_dir"] == "work"  # relative to the root that holds .gdkit/
    assert entry["params"]["explore_runs_checked"] == []


def test_the_model_card_shows_the_folder_searched(tmp_path):
    import json

    from gdkit.cards import model_card

    runs, project = _explore_then_registered(tmp_path, {"baseline": "b"}, {"baseline": "b"})
    _freeze(runs, "reg-1", "registered", project)
    setup = {"base_model_revision": "r", "tokenizer_sha256": "1" * 64, "config_sha256": "2" * 64,
             "eval_harness_sha256": "3" * 64, "prompt_template_sha256": "4" * 64}
    (runs / "reg-1" / "model_setup.json").write_text(json.dumps(setup), encoding="utf-8")
    assert model_card(runs / "reg-1")["preregistration_explore_runs_dir"] == "runs"


def test_removing_a_declaration_after_the_freeze_is_detected(tmp_path):
    runs, project = _explore_then_registered(
        tmp_path, {"refusal_threshold": 0.05},
        {"refusal_threshold": 0.03, CHANGED_AFTER_EXPLORE_KEY: ["refusal_threshold"]},
    )
    entry = _freeze(runs, "reg-1", "registered", project)
    assert check_answers([entry], project) == []
    _write(project, {"refusal_threshold": 0.03})
    problems = check_answers([entry], project)
    assert problems and CHANGED_AFTER_EXPLORE_KEY in problems[0]
