"""Non-interactive backend for the /overfit-clinic Claude Code skill.

The skill asks each question in chat, then calls this module. It never writes the project file itself.

    python -m gdkit.skill_api next --project P [--questions Q] [--mode M]
        prints JSON for the next unanswered question, or {"done": true, "problems": [...]}

    python -m gdkit.skill_api set --project P --id QID --value "TEXT" [--questions Q] [--mode M]
        validates one answer with the clinic's rules and saves it; prints JSON; exit 0 when saved, 1 when refused

    python -m gdkit.skill_api change --project P --id QID --value-file F [--questions Q] [--mode M]
        replaces a real answer and declares the change under ``changed_after_explore``

    python -m gdkit.skill_api declare --project P --id QID --list replaced_unset|changed_after_explore
        declares an answer without changing it, for what set and change cannot record: a question never
        asked during an earlier explore run, or an answer dropped since; the registered freeze names it

``set`` refuses to change an answer that is already in the file. In registered mode a blank value is
refused; in explore mode it is recorded under ``skipped_in_explore``. A required question left ``UNSET`` in
explore mode can be answered in registered mode. An answer to a question that was ``UNSET`` or skipped in
explore mode is added to the file's ``replaced_unset`` list. ``change`` is the only way to change a real
answer, and it is always declared, because the registered freeze refuses undeclared differences from an
earlier explore run.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from gdkit.clinic import _load_answers, _write_answers, parse_answer
from gdkit.interview import (
    CHANGED_AFTER_EXPLORE_KEY,
    MODES,
    SKIPPED_IN_EXPLORE_KEY,
    UNSET,
    is_open,
    load_questions,
    record_answer,
    record_change,
    REPLACED_UNSET_KEY,
    _append,
    record_skip,
    validate,
    was_undecided_in_explore,
)


def next_question(questions_data: dict, answers: dict[str, Any], mode: str) -> dict[str, Any]:
    for question in questions_data["questions"]:
        if is_open(question, answers, mode):
            return {
                "done": False,
                "id": question["id"],
                "ask": question["ask"],
                "why": question.get("why", ""),
                "type": question["type"],
                "values": question.get("values"),
                "required": mode in question.get("required_for", []),
                "mode": mode,
                "replaces_unset": was_undecided_in_explore(answers, question["id"]),
            }
    return {"done": True, "problems": validate(answers, questions_data, mode)}


def set_answer(questions_data: dict, project: Path, mode: str, qid: str, raw: str) -> tuple[bool, str]:
    questions = {q["id"]: q for q in questions_data["questions"]}
    if qid not in questions:
        return False, f"{qid}: not a question in the interview"
    answers = _load_answers(project)
    # A question skipped in explore mode is passed over by next(), but it can still be answered on purpose.
    skipped = qid in (answers.get(SKIPPED_IN_EXPLORE_KEY) or [])
    if not (is_open(questions[qid], answers, mode) or skipped):
        return False, (
            f"{qid}: already answered in {project}. To change it before a run starts, use change (the change is "
            "declared under changed_after_explore). After a run has started, start a new run"
        )
    status, value, message = parse_answer(questions[qid], raw, mode)
    if status == "error":
        return False, f"{qid}: {message}"
    if status == "skip":
        if mode == "explore":
            record_skip(answers, qid)
            _write_answers(project, answers)
            return True, f"skipped {qid}; listed under {SKIPPED_IN_EXPLORE_KEY}, and a later answer joins replaced_unset"
        return False, f"{qid}: a blank answer is not saved"
    record_answer(answers, qid, value)
    _write_answers(project, answers)
    return True, f"saved {qid}"


def change_answer(questions_data: dict, project: Path, mode: str, qid: str, raw: str) -> tuple[bool, str]:
    """Replace a real answer and declare it under changed_after_explore. The only way to change an answer."""
    questions = {q["id"]: q for q in questions_data["questions"]}
    if qid not in questions:
        return False, f"{qid}: not a question in the interview"
    answers = _load_answers(project)
    if qid not in answers or answers[qid] == UNSET:
        return False, f"{qid}: has no real answer to change; use set"
    status, value, message = parse_answer(questions[qid], raw, mode)
    if status == "error":
        return False, f"{qid}: {message}"
    if status == "skip" or value == UNSET:
        return False, f"{qid}: a change needs a real answer"
    if value == answers[qid]:
        return False, f"{qid}: the new answer is the same as the old one"
    record_change(answers, qid, value)
    _write_answers(project, answers)
    return True, f"changed {qid}; declared under {CHANGED_AFTER_EXPLORE_KEY}"


DECLARABLE = (REPLACED_UNSET_KEY, CHANGED_AFTER_EXPLORE_KEY)


def declare_answer(questions_data: dict, project: Path, qid: str, list_name: str) -> tuple[bool, str]:
    """Add ``qid`` to a declaration list without changing its answer.

    For the cases ``set`` and ``change`` cannot record: a question never asked during an earlier explore run
    (``replaced_unset``), or an answer dropped since then (``changed_after_explore``). The registered freeze
    names the command to run.
    """
    if qid not in {q["id"] for q in questions_data["questions"]}:
        return False, f"{qid}: not a question in the interview"
    if list_name not in DECLARABLE:
        return False, f"{list_name}: can only declare under {', '.join(DECLARABLE)}"
    answers = _load_answers(project)
    _append(answers, list_name, qid)
    _write_answers(project, answers)
    return True, f"declared {qid} under {list_name}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="gdkit skill_api")
    commands = parser.add_subparsers(dest="command", required=True)
    declare = commands.add_parser("declare", help="add a question to a declaration list without changing its answer")
    declare.add_argument("--project", default=".gdkit/project.yaml")
    declare.add_argument("--questions", default="interview/questions.yaml")
    declare.add_argument("--id", required=True)
    declare.add_argument("--list", required=True, choices=DECLARABLE, dest="list_name")
    for name in ("next", "set", "change"):
        sub = commands.add_parser(name)
        sub.add_argument("--project", default=".gdkit/project.yaml")
        sub.add_argument("--questions", default="interview/questions.yaml")
        sub.add_argument("--mode", choices=MODES, default="registered")
        if name in ("set", "change"):
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
        if args.command == "declare":
            ok, message = declare_answer(questions, project, args.id, args.list_name)
            print(json.dumps({"ok": ok, "message": message}, ensure_ascii=False))
            return 0 if ok else 1
        if args.value_file is not None:
            raw = Path(args.value_file).read_text(encoding="utf-8").rstrip("\r\n")
        else:
            raw = args.value
        command = set_answer if args.command == "set" else change_answer
        ok, message = command(questions, project, args.mode, args.id, raw)
        print(json.dumps({"ok": ok, "message": message}, ensure_ascii=False))
        return 0 if ok else 1
    except Exception as exc:  # the skill needs one JSON answer, not a traceback
        print(json.dumps({"ok": False, "message": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    sys.exit(main())
