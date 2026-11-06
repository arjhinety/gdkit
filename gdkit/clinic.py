"""The /overfit-clinic interview: asks the questions and writes ``.gdkit/project.yaml`` (Must 4).

    python -m gdkit.clinic [--project .gdkit/project.yaml] [--questions interview/questions.yaml] [--mode explore|registered]

Behaviour:
* questions are asked in file order, one at a time;
* each answer is checked with the same rules as ``gdkit doctor`` and asked again until it passes;
* the project file is rewritten after every answer, so an interrupted run resumes where it stopped;
* a question already answered in the file is not asked again;
* nothing is filled in by default. A blank answer is a skip (explore) or a refusal (registered);
* ``UNSET`` is accepted in explore mode, and for questions that are not required in registered mode;
* a yes to ``needs_paper_lookup`` checks whether ``pwc`` is on PATH and says what to do if it is not.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable

import yaml

from gdkit.interview import UNSET, MODES, InterviewError, _check_value, load_questions, validate

PWC_INSTALL_NOTE = (
    "pwc is not on PATH. Install the Papers With Code CLI from the pwc project's instructions, "
    "then rerun the clinic. Paper lookups are recorded in the ledger only while pwc is available. "
    "Missing pwc is recorded as UNAVAILABLE, and registered runs refuse it until it is retried."
)
_YES = {"yes", "y", "true"}
_NO = {"no", "n", "false"}


def _load_answers(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise InterviewError(f"{path} must be a mapping of question ID to answer")
    return data


def _write_answers(path: Path, answers: dict[str, Any]) -> None:
    """Replace the file atomically, so a crash never leaves half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".project-", suffix=".yaml")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        yaml.safe_dump(answers, handle, sort_keys=False, allow_unicode=True)
    os.replace(tmp, path)


def parse_answer(question: dict, raw: str, mode: str) -> tuple[str, Any, str]:
    """Turn typed text into a value. Returns (status, value, message): status is set, skip or error."""
    required = mode in question.get("required_for", [])
    text = raw.strip()
    if text == "":
        if required and mode == "registered":
            return "error", None, "this answer is required for registered runs"
        return "skip", None, ""
    if text == UNSET:
        if mode == "explore" or not required:
            return "set", UNSET, ""
        return "error", None, "UNSET is not allowed for this question in registered mode"

    kind = question["type"]
    if kind in ("text", "path"):
        value: Any = text
    elif kind == "number":
        try:
            value = float(text)
        except ValueError:
            return "error", None, "type a number, for example 0.05"
    elif kind == "enum":
        if text not in question["values"]:
            return "error", None, f"choose one of: {', '.join(question['values'])}"
        value = text
    elif kind == "list":
        value = [item.strip() for item in text.split(",") if item.strip()]
    elif kind == "sha256":
        value = text.lower()
    elif kind == "boolean":
        lowered = text.lower()
        if lowered in _YES:
            value = True
        elif lowered in _NO:
            value = False
        else:
            return "error", None, "answer yes or no"
    else:
        return "error", None, f"unknown question type {kind!r}"

    problems = _check_value(question["id"], question, value, required)
    if problems:
        return "error", None, problems[0].split(": ", 1)[1]
    return "set", value, ""


def run_interview(
    questions_path: str | Path,
    project_path: str | Path,
    mode: str,
    *,
    ask: Callable[[str], str] = input,
    out: Callable[[str], None] = print,
    which: Callable[[str], str | None] = shutil.which,
) -> list[str]:
    """Ask every unanswered question and save each answer. Returns the validation problems for ``mode``."""
    if mode not in MODES:
        raise InterviewError(f"mode must be one of {MODES}, got {mode!r}")
    questions_data = load_questions(questions_path)
    project = Path(project_path)
    answers = _load_answers(project)

    for question in questions_data["questions"]:
        qid = question["id"]
        if qid in answers:
            continue
        required = mode in question.get("required_for", [])
        out("")
        out(f"[{qid}] {question['ask']}")
        if question.get("why"):
            out(f"  why: {question['why']}")
        while True:
            status, value, message = parse_answer(question, ask("> "), mode)
            if status == "error":
                out(f"  {message}")
                continue
            if status == "skip":
                break
            answers[qid] = value
            _write_answers(project, answers)
            if qid == "needs_paper_lookup" and value is True and which("pwc") is None:
                out(f"  {PWC_INSTALL_NOTE}")
            break

    problems = validate(answers, questions_data, mode)
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="gdkit clinic")
    parser.add_argument("--project", default=".gdkit/project.yaml")
    parser.add_argument("--questions", default="interview/questions.yaml")
    parser.add_argument("--mode", choices=MODES, default="explore")
    args = parser.parse_args(argv)
    try:
        problems = run_interview(args.questions, args.project, args.mode)
    except (KeyboardInterrupt, EOFError):
        print("\nstopped. Answers so far are saved, and the clinic resumes from them next time.", file=sys.stderr)
        return 1
    except (OSError, InterviewError, yaml.YAMLError) as exc:
        print(f"clinic cannot continue: {exc}", file=sys.stderr)
        return 1
    if problems:
        print("")
        for problem in problems:
            print(problem)
        print(f"{len(problems)} problem(s) for {args.mode} mode. Run the clinic again to answer them.", file=sys.stderr)
        return 1
    print(f"\nAll answers are acceptable for {args.mode} mode.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
