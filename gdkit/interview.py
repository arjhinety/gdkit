"""Validate ``.gdkit/project.yaml`` against ``interview/questions.yaml`` (Must 3).

The questions file is the schema. For each question:
* an answer is checked against its ``type``;
* in ``registered`` mode a question listed in ``required_for`` must have an answer, and that answer may not
  be ``UNSET``;
* in ``explore`` mode a required question may be ``UNSET``, and a missing answer is allowed;
* a key that is not a question is refused, so a typo cannot pass silently.

Three keys are reserved bookkeeping, not questions. They exist because an answer given after an explore run
may have been chosen after seeing the data, and the answers alone cannot tell that apart from an answer
decided up front:

* ``skipped_in_explore``: questions left blank in explore mode and not answered since;
* ``replaced_unset``: questions that were undecided in explore mode (``UNSET`` or skipped) and were answered
  later, in the order they were answered;
* ``changed_after_explore``: questions whose real answer was deliberately changed with ``skill_api change``.

The clinic and the skill backend write them. ``freeze_run`` copies the last two into the preregistration entry
and, for a registered run, refuses any difference from an earlier explore freeze that they do not declare.

No answer has a default. Validation only checks what was written down.
"""

from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any

import yaml

UNSET = "UNSET"
MODES = ("explore", "registered")
TYPES = ("text", "number", "enum", "path", "list", "sha256", "boolean")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
REPLACED_UNSET_KEY = "replaced_unset"
SKIPPED_IN_EXPLORE_KEY = "skipped_in_explore"
CHANGED_AFTER_EXPLORE_KEY = "changed_after_explore"
RESERVED_KEYS = (REPLACED_UNSET_KEY, SKIPPED_IN_EXPLORE_KEY, CHANGED_AFTER_EXPLORE_KEY)


class InterviewError(ValueError):
    """The questions file itself is malformed."""


def check_questions(data: Any) -> list[str]:
    """Problems in the questions file itself. Empty means it is well formed."""
    if not isinstance(data, dict) or not isinstance(data.get("questions"), list):
        return ["the questions file must have a 'questions' list"]
    problems: list[str] = []
    seen: set[str] = set()
    for question in data["questions"]:
        qid = question.get("id") if isinstance(question, dict) else None
        if not qid:
            problems.append(f"a question has no id: {question!r}")
            continue
        if qid in RESERVED_KEYS:
            problems.append(f"{qid}: this id is reserved for gdkit's record of answers given after an explore run")
        if qid in seen:
            problems.append(f"{qid}: duplicate id")
        seen.add(qid)
        if question.get("type") not in TYPES:
            problems.append(f"{qid}: unknown type {question.get('type')!r}")
        if not set(question.get("required_for", [])) <= set(MODES):
            problems.append(f"{qid}: required_for must use {MODES}")
        if question.get("type") == "enum" and not question.get("values"):
            problems.append(f"{qid}: an enum needs values")
    return problems


def load_questions(path: str | Path) -> dict:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    problems = check_questions(data)
    if problems:
        raise InterviewError("; ".join(problems))
    return data


def _check_value(qid: str, question: dict, value: Any, required: bool) -> list[str]:
    kind = question["type"]
    if kind == "text":
        ok = isinstance(value, str) and value.strip() != ""
        return [] if ok else [f"{qid}: expected non-empty text"]
    if kind == "number":
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return [f"{qid}: expected a number"]
        if isinstance(value, int):  # ints are always finite, and math.isfinite overflows on huge ones
            return []
        return [] if math.isfinite(value) else [f"{qid}: expected a finite number, got {value!r}"]
    if kind == "enum":
        return [] if value in question["values"] else [f"{qid}: {value!r} is not one of {question['values']}"]
    if kind == "path":
        ok = isinstance(value, str) and value.strip() != ""
        return [] if ok else [f"{qid}: expected a path"]
    if kind == "list":
        if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
            return [f"{qid}: expected a list of non-empty strings"]
        if required and not value:
            return [f"{qid}: the list may not be empty"]
        return []
    if kind == "sha256":
        ok = isinstance(value, str) and _SHA256.match(value) is not None
        return [] if ok else [f"{qid}: expected a lowercase 64-character sha256"]
    if kind == "boolean":
        return [] if isinstance(value, bool) else [f"{qid}: expected true or false"]
    return [f"{qid}: unknown type {kind!r}"]


def is_open(question: dict, answers: dict[str, Any], mode: str) -> bool:
    """Whether the interview should still ask ``question``.

    A missing answer is open, except in explore mode after it was skipped there (asking again would loop).
    An ``UNSET`` answer is open again in registered mode when the question is required there, so a study that
    started in explore mode can be completed. Any other answer is closed.
    """
    qid = question["id"]
    if qid not in answers:
        return not (mode == "explore" and qid in (answers.get(SKIPPED_IN_EXPLORE_KEY) or []))
    return answers[qid] == UNSET and mode == "registered" and mode in question.get("required_for", [])


def was_undecided_in_explore(answers: dict[str, Any], qid: str) -> bool:
    """Whether ``qid`` is currently ``UNSET`` or listed as skipped in explore mode."""
    return answers.get(qid) == UNSET or qid in (answers.get(SKIPPED_IN_EXPLORE_KEY) or [])


def _append(answers: dict[str, Any], key: str, qid: str) -> None:
    items = list(answers.get(key) or [])
    if qid not in items:
        items.append(qid)
    answers[key] = items


def record_answer(answers: dict[str, Any], qid: str, value: Any) -> None:
    """Store ``value`` for ``qid``. When it settles a question undecided in explore mode (``UNSET`` or
    skipped), add ``qid`` to the replaced_unset list."""
    skipped = list(answers.get(SKIPPED_IN_EXPLORE_KEY) or [])
    if value != UNSET and was_undecided_in_explore(answers, qid):
        _append(answers, REPLACED_UNSET_KEY, qid)
    if qid in skipped:
        skipped.remove(qid)
        if skipped:
            answers[SKIPPED_IN_EXPLORE_KEY] = skipped
        else:
            answers.pop(SKIPPED_IN_EXPLORE_KEY)
    answers[qid] = value


def record_skip(answers: dict[str, Any], qid: str) -> None:
    """Note that ``qid`` was left blank in explore mode."""
    _append(answers, SKIPPED_IN_EXPLORE_KEY, qid)


def record_change(answers: dict[str, Any], qid: str, value: Any) -> None:
    """Replace a real answer and add ``qid`` to the changed_after_explore list."""
    _append(answers, CHANGED_AFTER_EXPLORE_KEY, qid)
    answers[qid] = value


def split_answers(data: dict[str, Any]) -> tuple[dict[str, Any], dict[str, list[str]]]:
    """(question answers, {bookkeeping key: list}) from a project file's mapping."""
    answers = {key: value for key, value in data.items() if key not in RESERVED_KEYS}
    return answers, {key: list(data.get(key) or []) for key in RESERVED_KEYS}


def validate(answers: dict[str, Any], questions_data: dict, mode: str) -> list[str]:
    """Problems with the answers for a run in ``mode``. Empty means the project file is acceptable."""
    if mode not in MODES:
        raise InterviewError(f"mode must be one of {MODES}, got {mode!r}")
    questions = {q["id"]: q for q in questions_data["questions"]}
    problems = [
        f"{key}: not a question in the interview"
        for key in answers
        if key not in questions and key not in RESERVED_KEYS
    ]
    for key in RESERVED_KEYS:
        listed = answers.get(key, [])
        if not isinstance(listed, list) or not all(isinstance(q, str) and q in questions for q in listed):
            problems.append(f"{key}: expected a list of question IDs, got {listed!r}")
    for qid, question in questions.items():
        required = mode in question.get("required_for", [])
        value = answers.get(qid)
        if value is None:
            if required and mode == "registered":
                problems.append(f"{qid}: missing (required for {mode})")
            elif required:
                problems.append(f"{qid}: not answered yet (required for registered runs; explore may leave it UNSET)")
            continue
        if value == UNSET:
            if required and mode == "registered":
                problems.append(f"{qid}: UNSET is not allowed in registered mode")
            continue
        problems.extend(_check_value(qid, question, value, required))
    return problems
