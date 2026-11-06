"""Validate ``.gdkit/project.yaml`` against ``interview/questions.yaml`` (Must 3).

The questions file is the schema. For each question:
* an answer is checked against its ``type``;
* in ``registered`` mode a question listed in ``required_for`` must have an answer, and that answer may not
  be ``UNSET``;
* in ``explore`` mode a required question may be ``UNSET``, and a missing answer is allowed;
* a key that is not a question is refused, so a typo cannot pass silently.

No answer has a default. Validation only checks what was written down.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

UNSET = "UNSET"
MODES = ("explore", "registered")
TYPES = ("text", "number", "enum", "path", "list", "sha256", "boolean")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


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
        ok = isinstance(value, (int, float)) and not isinstance(value, bool)
        return [] if ok else [f"{qid}: expected a number"]
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


def validate(answers: dict[str, Any], questions_data: dict, mode: str) -> list[str]:
    """Problems with the answers for a run in ``mode``. Empty means the project file is acceptable."""
    if mode not in MODES:
        raise InterviewError(f"mode must be one of {MODES}, got {mode!r}")
    questions = {q["id"]: q for q in questions_data["questions"]}
    problems = [f"{key}: not a question in the interview" for key in answers if key not in questions]
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
