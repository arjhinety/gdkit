from pathlib import Path

import pytest
import yaml

from gdkit.clinic import parse_answer, run_interview
from gdkit.interview import UNSET, load_questions, validate

ROOT = Path(__file__).resolve().parents[1]
QUESTIONS_PATH = ROOT / "interview" / "questions.yaml"
QUESTIONS = load_questions(QUESTIONS_PATH)
BY_ID = {q["id"]: q for q in QUESTIONS["questions"]}


def scripted(answers):
    """An ask() that returns the scripted answers in order, then fails if the clinic asks for more."""
    queue = list(answers)

    def ask(prompt):
        if not queue:
            raise AssertionError("the clinic asked more questions than were scripted")
        return queue.pop(0)

    return ask


def registered_script():
    """One reply per question, in file order: valid answers for the registered ones, blank for optional ones."""
    replies = {
        "study_question": "Does the refusal rate stay below 5 percent?",
        "claim_type": "capability",
        "raw_data_source": "dataset-id@rev-abc",
        "raw_data_revision_sha256": "a" * 64,
        "data_license": "CC-BY-4.0",
        "base_model_revision": "15852e8c",
        "eval_harness_sha256": "b" * 64,
        "prompt_template_sha256": "c" * 64,
        "primary_metric": "call_f1 on the held-out set",
        "kill_criteria": "call_f1 below 0.70",
        "general_capability_suite": "IFEval, GSM8K, MMLU-Pro",
        "general_capability_margin": "0.02 absolute",
        "refusal_threshold": "0.05",
        "expensive_gates": "train, judge",
        "needs_paper_lookup": "no",
        "baseline": "the parent model at 0.70 call_f1",
        "split_protocol": "time-based 80/10/10, frozen before modelling",
        "test_access": "no",
        "uncertainty_method": "3 seeds, standard deviation",
        "evaluation_slices": "groups A and B",
        "intended_use": "tool-call routing in the support bot",
        "owner_signer": "test owner",
    }
    return [replies.get(q["id"], "") for q in QUESTIONS["questions"]]


def test_a_full_registered_interview_writes_a_valid_file(tmp_path):
    project = tmp_path / ".gdkit" / "project.yaml"
    out = []
    problems = run_interview(QUESTIONS_PATH, project, "registered", ask=scripted(registered_script()),
                             out=out.append)
    assert problems == []
    saved = yaml.safe_load(project.read_text(encoding="utf-8"))
    assert validate(saved, QUESTIONS, "registered") == []
    assert saved["refusal_threshold"] == 0.05
    assert saved["expensive_gates"] == ["train", "judge"]
    assert saved["raw_data_revision_sha256"] == "a" * 64


def test_blank_required_answer_in_registered_mode_is_asked_again(tmp_path):
    project = tmp_path / "project.yaml"
    script = registered_script()
    first = list(QUESTIONS["questions"])[0]
    assert first["id"] == "study_question"
    script = [""] + script
    out = []
    run_interview(QUESTIONS_PATH, project, "registered", ask=scripted(script), out=out.append)
    assert any("required for registered runs" in line for line in out)


def test_an_invalid_enum_is_asked_again_until_it_passes(tmp_path):
    project = tmp_path / "project.yaml"
    script = registered_script()
    script.insert(1, "vibes")  # claim_type is the second question
    out = []
    run_interview(QUESTIONS_PATH, project, "registered", ask=scripted(script), out=out.append)
    assert any("choose one of" in line for line in out)
    assert yaml.safe_load(project.read_text(encoding="utf-8"))["claim_type"] == "capability"


def test_explore_mode_accepts_unset_for_a_required_question(tmp_path):
    project = tmp_path / "project.yaml"
    script = [UNSET] * len(QUESTIONS["questions"])
    run_interview(QUESTIONS_PATH, project, "explore", ask=scripted(script), out=lambda _: None)
    saved = yaml.safe_load(project.read_text(encoding="utf-8"))
    assert saved["study_question"] == UNSET
    assert validate(saved, QUESTIONS, "explore") == []


def test_registered_mode_refuses_unset_for_a_required_question(tmp_path):
    status, _, message = parse_answer(BY_ID["baseline"], UNSET, "registered")
    assert status == "error" and "registered" in message


def test_unset_is_fine_for_an_optional_question_in_registered_mode():
    status, value, _ = parse_answer(BY_ID["needs_paper_lookup"], UNSET, "registered")
    assert (status, value) == ("set", UNSET)


def test_answers_already_in_the_file_are_not_asked_again(tmp_path):
    project = tmp_path / "project.yaml"
    project.write_text(yaml.safe_dump({"study_question": "Is it better?"}), encoding="utf-8")
    script = registered_script()[1:]  # every question except study_question
    run_interview(QUESTIONS_PATH, project, "registered", ask=scripted(script), out=lambda _: None)
    saved = yaml.safe_load(project.read_text(encoding="utf-8"))
    assert saved["study_question"] == "Is it better?"
    assert validate(saved, QUESTIONS, "registered") == []


def test_no_default_is_written_for_a_skipped_question(tmp_path):
    project = tmp_path / "project.yaml"
    script = [""] * len(QUESTIONS["questions"])  # blank everything, explore mode skips it
    run_interview(QUESTIONS_PATH, project, "explore", ask=scripted(script), out=lambda _: None)
    assert not project.exists() or yaml.safe_load(project.read_text(encoding="utf-8")) in (None, {})


def test_yes_to_paper_lookup_without_pwc_prints_the_install_note(tmp_path):
    project = tmp_path / "project.yaml"
    script = [
        "yes" if q["id"] == "needs_paper_lookup" else reply
        for q, reply in zip(QUESTIONS["questions"], registered_script())
    ]
    out = []
    run_interview(QUESTIONS_PATH, project, "registered", ask=scripted(script), out=out.append,
                  which=lambda name: None)
    assert any("pwc is not on PATH" in line for line in out)
    assert yaml.safe_load(project.read_text(encoding="utf-8"))["needs_paper_lookup"] is True
