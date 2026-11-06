"""Non-interactive backend for the /overfit-clinic Claude Code skill.

The skill asks each question in chat, then calls this module. It never writes the project file itself.

    python -m gdkit.skill_api next --project P [--questions Q] [--mode M]
        prints JSON for the next unanswered question, or {"done": true, "problems": [...]}

    python -m gdkit.skill_api set --project P --id QID --value "TEXT" [--questions Q] [--mode M]
        validates one answer with the clinic's rules and saves it; prints JSON; exit 0 when saved, 1 when refused

``set`` refuses to change an answer that is already in the file. A blank value is refused, not saved.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from gdkit.clinic import _load_answers, _write_answers, parse_answer
from gdkit.interview import MODES, load_questions, validate


def next_question(questions_data: dict, answers: dict[str, Any], mode: str) -> dict[str, Any]:
    for question in questions_data["questions"]:
        if question["id"] not in answers:
            return {
                "done": False,
                "id": question["id"],
                "ask": question["ask"],
                "why": question.get("why", ""),
                "type": question["type"],
                "values": question.get("values"),
                "required": mode in question.get("required_for", []),
                "mode": mode,
            }
    return {"done": True, "problems": validate(answers, questions_data, mode)}


def set_answer(questions_data: dict, project: Path, mode: str, qid: str, raw: str) -> tuple[bool, str]:
    questions = {q["id"]: q for q in questions_data["questions"]}
    if qid not in questions:
        return False, f"{qid}: not a question in the interview"
    answers = _load_answers(project)
    if qid in answers:
        return False, f"{qid}: already answered in {project}. Answers are frozen when a run starts; to change one, start a new run"
    status, value, message = parse_answer(questions[qid], raw, mode)
    if status == "error":
        return False, f"{qid}: {message}"
    if status == "skip":
        return False, f"{qid}: a blank answer is not saved"
    answers[qid] = value
    _write_answers(project, answers)
    return True, f"saved {qid}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="gdkit skill_api")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("next", "set"):
        sub = commands.add_parser(name)
        sub.add_argument("--project", default=".gdkit/project.yaml")
        sub.add_argument("--questions", default="interview/questions.yaml")
        sub.add_argument("--mode", choices=MODES, default="registered")
        if name == "set":
            sub.add_argument("--id", required=True)
            source = sub.add_mutually_exclusive_group(required=True)
            source.add_argument("--value", help="the answer on the command line (quote carefully)")
            source.add_argument("--value-file", help="a file holding the answer, safer for punctuation")
    args = parser.parse_args(argv)

    try:
        questions = load_questions(args.questions)
        project = Path(args.project)
        if args.command == "next":
            print(json.dumps(next_question(questions, _load_answers(project), args.mode), ensure_ascii=False))
            return 0
        if args.value_file is not None:
            raw = Path(args.value_file).read_text(encoding="utf-8").rstrip("\r\n")
        else:
            raw = args.value
        ok, message = set_answer(questions, project, args.mode, args.id, raw)
        print(json.dumps({"ok": ok, "message": message}, ensure_ascii=False))
        return 0 if ok else 1
    except Exception as exc:  # the skill needs one JSON answer, not a traceback
        print(json.dumps({"ok": False, "message": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    sys.exit(main())
