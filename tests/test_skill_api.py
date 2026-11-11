import json
import subprocess
import sys
from pathlib import Path

import yaml

from gdkit.interview import UNSET, load_questions
from gdkit.skill_api import next_question, set_answer

ROOT = Path(__file__).resolve().parents[1]
QUESTIONS_PATH = ROOT / "interview" / "questions.yaml"
QUESTIONS = load_questions(QUESTIONS_PATH)


def test_next_returns_the_first_unanswered_question(tmp_path):
    result = next_question(QUESTIONS, {}, "registered")
    assert result["done"] is False
    assert result["id"] == "study_question"
    assert result["required"] is True
    assert result["mode"] == "registered"


def test_next_skips_questions_already_answered():
    result = next_question(QUESTIONS, {"study_question": "Is it better?"}, "registered")
    assert result["id"] == "claim_type"


def test_next_reports_done_with_problems_when_everything_is_answered():
    answers = {q["id"]: "x" for q in QUESTIONS["questions"]}
    result = next_question(QUESTIONS, answers, "registered")
    assert result["done"] is True
    assert result["problems"]  # "x" is not a valid answer for every question type


def test_next_in_registered_mode_reopens_a_required_unset_and_says_so():
    result = next_question(QUESTIONS, {q["id"]: UNSET for q in QUESTIONS["questions"]}, "registered")
    assert result["done"] is False and result["required"] is True
    assert result["replaces_unset"] is True
    assert next_question(QUESTIONS, {q["id"]: UNSET for q in QUESTIONS["questions"]}, "explore")["done"] is True


def test_set_saves_a_valid_answer(tmp_path):
    project = tmp_path / "project.yaml"
    ok, message = set_answer(QUESTIONS, project, "registered", "refusal_threshold", "0.05")
    assert ok, message
    assert yaml.safe_load(project.read_text(encoding="utf-8"))["refusal_threshold"] == 0.05


def test_set_refuses_an_invalid_answer_and_saves_nothing(tmp_path):
    project = tmp_path / "project.yaml"
    ok, message = set_answer(QUESTIONS, project, "registered", "claim_type", "vibes")
    assert not ok and "choose one of" in message
    assert not project.exists()


def test_set_refuses_a_blank_answer(tmp_path):
    project = tmp_path / "project.yaml"
    ok, message = set_answer(QUESTIONS, project, "registered", "baseline", "   ")
    assert not ok and "required" in message
    assert not project.exists()


def test_set_does_not_save_a_blank_optional_answer(tmp_path):
    project = tmp_path / "project.yaml"
    ok, message = set_answer(QUESTIONS, project, "registered", "needs_paper_lookup", "")
    assert not ok and "not saved" in message
    assert not project.exists()


def test_set_refuses_to_change_an_existing_answer(tmp_path):
    project = tmp_path / "project.yaml"
    set_answer(QUESTIONS, project, "registered", "baseline", "parent at 0.70")
    ok, message = set_answer(QUESTIONS, project, "registered", "baseline", "a different baseline")
    assert not ok and "already answered" in message
    assert yaml.safe_load(project.read_text(encoding="utf-8"))["baseline"] == "parent at 0.70"


def test_set_refuses_an_unknown_question(tmp_path):
    ok, message = set_answer(QUESTIONS, tmp_path / "p.yaml", "registered", "basline", "x")
    assert not ok and "not a question" in message


def test_the_cli_prints_json_for_next_and_set(tmp_path):
    import os

    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    project = tmp_path / "project.yaml"
    base = [sys.executable, "-m", "gdkit.skill_api", "--help"]
    subprocess.run(base, capture_output=True, text=True, env=env)  # sanity: module imports

    nxt = subprocess.run(
        [sys.executable, "-m", "gdkit.skill_api", "next", "--project", str(project),
         "--questions", str(QUESTIONS_PATH), "--mode", "registered"],
        capture_output=True, text=True, env=env,
    )
    assert nxt.returncode == 0
    assert json.loads(nxt.stdout)["id"] == "study_question"

    saved = subprocess.run(
        [sys.executable, "-m", "gdkit.skill_api", "set", "--project", str(project),
         "--questions", str(QUESTIONS_PATH), "--mode", "registered", "--id", "study_question",
         "--value", "Does refusal stay under 5 percent?"],
        capture_output=True, text=True, env=env,
    )
    assert saved.returncode == 0 and json.loads(saved.stdout)["ok"] is True

    refused = subprocess.run(
        [sys.executable, "-m", "gdkit.skill_api", "set", "--project", str(project),
         "--questions", str(QUESTIONS_PATH), "--mode", "registered", "--id", "claim_type", "--value", "vibes"],
        capture_output=True, text=True, env=env,
    )
    assert refused.returncode == 1 and json.loads(refused.stdout)["ok"] is False


def test_set_reads_the_answer_from_a_file_with_punctuation(tmp_path):
    import os

    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    project = tmp_path / "project.yaml"
    answer = tmp_path / "answer.txt"
    answer.write_text('Does "refusal" stay under 5%? It\'s $1 & more; yes\n', encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-m", "gdkit.skill_api", "set", "--project", str(project),
         "--questions", str(QUESTIONS_PATH), "--mode", "registered", "--id", "study_question",
         "--value-file", str(answer)],
        capture_output=True, text=True, env=env,
    )
    assert result.returncode == 0, result.stdout
    saved = yaml.safe_load(project.read_text(encoding="utf-8"))
    assert saved["study_question"] == 'Does "refusal" stay under 5%? It\'s $1 & more; yes'


def test_explore_unset_answers_are_asked_again_in_registered_mode(tmp_path):
    project = tmp_path / "project.yaml"
    project.write_text(yaml.safe_dump({"study_question": UNSET, "baseline": "parent at 0.70"}), encoding="utf-8")
    answers = yaml.safe_load(project.read_text(encoding="utf-8"))
    assert next_question(QUESTIONS, answers, "registered")["id"] == "study_question"
    ok, message = set_answer(QUESTIONS, project, "registered", "study_question", "Is it better?")
    assert ok, message
    assert yaml.safe_load(project.read_text(encoding="utf-8"))["study_question"] == "Is it better?"
    # A real answer is still frozen.
    ok, message = set_answer(QUESTIONS, project, "registered", "baseline", "something else")
    assert not ok and "already answered" in message


def test_explore_mode_keeps_unset_answers(tmp_path):
    project = tmp_path / "project.yaml"
    project.write_text(yaml.safe_dump({"study_question": UNSET}), encoding="utf-8")
    assert next_question(QUESTIONS, {"study_question": UNSET}, "explore")["id"] != "study_question"
    ok, message = set_answer(QUESTIONS, project, "explore", "study_question", "Is it better?")
    assert not ok and "already answered" in message


def test_unset_on_an_optional_question_is_not_asked_again_in_registered_mode():
    answers = {"needs_paper_lookup": UNSET}
    assert next_question(QUESTIONS, answers, "registered")["id"] != "needs_paper_lookup"


def test_replacing_an_explore_unset_is_recorded(tmp_path):
    from gdkit.interview import REPLACED_UNSET_KEY, validate

    project = tmp_path / "project.yaml"
    project.write_text(yaml.safe_dump({"study_question": UNSET, "baseline": UNSET}), encoding="utf-8")
    set_answer(QUESTIONS, project, "registered", "baseline", "parent at 0.70")
    set_answer(QUESTIONS, project, "registered", "study_question", "Is it better?")
    saved = yaml.safe_load(project.read_text(encoding="utf-8"))
    assert saved[REPLACED_UNSET_KEY] == ["baseline", "study_question"]
    assert not any(p.startswith(REPLACED_UNSET_KEY) for p in validate(saved, QUESTIONS, "registered"))


def test_a_first_answer_is_not_recorded_as_a_replacement(tmp_path):
    from gdkit.interview import REPLACED_UNSET_KEY

    project = tmp_path / "project.yaml"
    set_answer(QUESTIONS, project, "registered", "baseline", "parent at 0.70")
    assert REPLACED_UNSET_KEY not in yaml.safe_load(project.read_text(encoding="utf-8"))
