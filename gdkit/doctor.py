"""``gdkit doctor``: check a project file against the interview questions.

    python -m gdkit.doctor [--project .gdkit/project.yaml] [--questions interview/questions.yaml] [--mode registered]

Exit status 0 when the file is acceptable for the mode, 1 otherwise. Each problem is printed on its own line.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

from gdkit.interview import MODES, InterviewError, load_questions, validate


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="gdkit doctor")
    parser.add_argument("--project", default=".gdkit/project.yaml")
    parser.add_argument("--questions", default="interview/questions.yaml")
    parser.add_argument("--mode", choices=MODES, default="registered")
    args = parser.parse_args(argv)

    try:
        questions = load_questions(args.questions)
    except (OSError, InterviewError, yaml.YAMLError) as exc:
        print(f"cannot read the questions file: {exc}", file=sys.stderr)
        return 1
    project = Path(args.project)
    if not project.exists():
        print(f"{project} does not exist; run the interview first", file=sys.stderr)
        return 1
    try:
        answers = yaml.safe_load(project.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        print(f"{project} is not valid YAML: {exc}", file=sys.stderr)
        return 1
    if not isinstance(answers, dict):
        print(f"{project} must be a mapping of question ID to answer", file=sys.stderr)
        return 1

    problems = validate(answers, questions, args.mode)
    for problem in problems:
        print(problem)
    if problems:
        print(f"{len(problems)} problem(s) for {args.mode} mode", file=sys.stderr)
        return 1
    print(f"OK for {args.mode} mode")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
