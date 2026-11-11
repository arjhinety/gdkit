import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from gdkit.interview import UNSET, check_questions, load_questions, validate

ROOT = Path(__file__).resolve().parents[1]
QUESTIONS_PATH = ROOT / "interview" / "questions.yaml"
QUESTIONS = load_questions(QUESTIONS_PATH)
BY_ID = {q["id"]: q for q in QUESTIONS["questions"]}


def sample(question):
    kind = question["type"]
    return {
        "text": "a value",
        "number": 1.5,
        "enum": question["values"][0] if question.get("values") else None,
        "path": "data/raw.csv",
        "list": ["one"],
        "sha256": "a" * 64,
        "boolean": True,
    }[kind]


def complete_registered():
    """Every question required for registered mode, with a valid value."""
    return {q["id"]: sample(q) for q in QUESTIONS["questions"] if "registered" in q.get("required_for", [])}


def test_the_draft_questions_file_is_well_formed():
    assert check_questions(QUESTIONS) == []


def test_a_complete_registered_project_passes():
    assert validate(complete_registered(), QUESTIONS, "registered") == []


def test_registered_mode_refuses_unset_on_a_required_question():
    answers = complete_registered()
    answers["baseline"] = UNSET
    problems = validate(answers, QUESTIONS, "registered")
    assert any(p.startswith("baseline:") and "UNSET" in p for p in problems)


def test_explore_mode_allows_unset_on_a_required_question():
    answers = {"study_question": UNSET, "baseline": UNSET}
    problems = validate(answers, QUESTIONS, "explore")
    assert not any(p.startswith(("study_question:", "baseline:")) and "UNSET" in p for p in problems)


def test_registered_mode_refuses_a_missing_required_answer():
    answers = complete_registered()
    del answers["split_protocol"]
    problems = validate(answers, QUESTIONS, "registered")
    assert any(p.startswith("split_protocol:") and "missing" in p for p in problems)


def test_explore_mode_does_not_require_missing_answers():
    assert validate({}, QUESTIONS, "explore") == []


def test_an_optional_question_may_be_unset_in_registered_mode():
    answers = complete_registered()
    answers["needs_paper_lookup"] = UNSET
    assert validate(answers, QUESTIONS, "registered") == []


def test_unknown_keys_are_refused():
    answers = complete_registered()
    answers["basline"] = "typo"
    problems = validate(answers, QUESTIONS, "registered")
    assert "basline: not a question in the interview" in problems


@pytest.mark.parametrize(
    "question_id, bad_value",
    [
        ("refusal_threshold", "0.1"),  # a number written as text
        ("raw_data_revision_sha256", "ABC"),  # not 64 lowercase hex characters
        ("claim_type", "vibes"),  # not one of the enum values
        ("needs_paper_lookup", "yes"),  # a boolean written as text
        ("evaluation_slices", []),  # empty list where one is required
        ("study_question", "   "),  # blank text
    ],
)
def test_wrong_values_are_refused(question_id, bad_value):
    answers = complete_registered()
    answers[question_id] = bad_value
    problems = validate(answers, QUESTIONS, "registered")
    assert any(p.startswith(f"{question_id}:") for p in problems), problems


def test_a_malformed_questions_file_is_detected():
    broken = {"questions": [{"id": "a", "type": "text", "required_for": ["registered"]},
                            {"id": "a", "type": "guess", "required_for": ["nowhere"]}]}
    problems = check_questions(broken)
    assert any("duplicate" in p for p in problems)
    assert any("unknown type" in p for p in problems)
    assert any("required_for" in p for p in problems)


def test_doctor_exit_status(tmp_path):
    env = {"PYTHONPATH": str(ROOT)}
    import os

    env = {**os.environ, **env}
    cmd = [sys.executable, "-m", "gdkit.doctor", "--questions", str(QUESTIONS_PATH)]

    good = tmp_path / "good.yaml"
    good.write_text(yaml.safe_dump(complete_registered()), encoding="utf-8")
    ok = subprocess.run(cmd + ["--project", str(good), "--mode", "registered"], capture_output=True, text=True, env=env)
    assert ok.returncode == 0, ok.stdout + ok.stderr

    bad = tmp_path / "bad.yaml"
    answers = complete_registered()
    answers["primary_metric"] = UNSET
    bad.write_text(yaml.safe_dump(answers), encoding="utf-8")
    refused = subprocess.run(cmd + ["--project", str(bad), "--mode", "registered"],
                             capture_output=True, text=True, env=env)
    assert refused.returncode == 1
    assert "primary_metric" in refused.stdout


@pytest.mark.parametrize("bad_value", [float("nan"), float("inf")])
def test_a_non_finite_number_in_the_file_is_refused(bad_value):
    answers = complete_registered()
    answers["refusal_threshold"] = bad_value
    problems = validate(answers, QUESTIONS, "registered")
    assert any(p.startswith("refusal_threshold:") and "finite" in p for p in problems), problems


def test_replaced_unset_must_name_questions():
    from gdkit.interview import REPLACED_UNSET_KEY

    answers = complete_registered()
    answers[REPLACED_UNSET_KEY] = ["baseline"]
    assert validate(answers, QUESTIONS, "registered") == []
    answers[REPLACED_UNSET_KEY] = ["basline"]
    assert any(p.startswith(REPLACED_UNSET_KEY) for p in validate(answers, QUESTIONS, "registered"))
    answers[REPLACED_UNSET_KEY] = "baseline"
    assert any(p.startswith(REPLACED_UNSET_KEY) for p in validate(answers, QUESTIONS, "registered"))


def test_a_question_may_not_use_the_reserved_id():
    from gdkit.interview import REPLACED_UNSET_KEY

    data = {"questions": [{"id": REPLACED_UNSET_KEY, "type": "text", "ask": "x"}]}
    assert any("reserved" in p for p in check_questions(data))
