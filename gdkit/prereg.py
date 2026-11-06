"""Pre-registration freeze: the run's answers are recorded when it starts, and checked before it is promoted.

``freeze_run`` writes the run's first ledger entry, ``op = "preregistration"``, with the project answers
verbatim and their sha256. It refuses if any data entry already exists, because an answer fixed after
the first data action is not pre-registered.

``check_answers`` compares the project file against that entry. Any answer that differs is named, and the
run is refused. A run with no preregistration entry is refused too.

This is detection, not prevention. Whoever can rewrite the ledger can also rewrite the entry. The git
history of the run and the attestation make a rewrite visible.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import yaml

from gdkit.interview import validate
from gdkit.ledger import Ledger
from gdkit.stream_hash import canonical_json_bytes

PREREG_OP = "preregistration"


class PreregError(ValueError):
    """The freeze cannot be taken, or the answers no longer match the frozen ones."""


def answers_digest(answers: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json_bytes(answers)).hexdigest()


def _read_answers(project_path: str | Path) -> dict[str, Any]:
    path = Path(project_path)
    if not path.exists():
        raise PreregError(f"{path} does not exist; run the clinic before starting the run")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise PreregError(f"{path} must be a mapping of question ID to answer")
    return data


def freeze_run(
    ledger: Ledger,
    project_path: str | Path,
    *,
    questions_data: dict | None = None,
) -> dict[str, Any]:
    """Record the project answers as the run's first ledger entry. Returns the stored entry."""
    if ledger.path.exists() and ledger.path.stat().st_size > 0:
        raise PreregError("the run already has ledger entries; the freeze must come before the first data action")
    answers = _read_answers(project_path)
    if questions_data is not None:
        problems = validate(answers, questions_data, ledger.mode)
        if problems:
            raise PreregError("the answers are not acceptable to freeze: " + "; ".join(problems))
    digest = answers_digest(answers)
    hashes = {"input_hash": digest, "output_hash": digest} if ledger.mode == "registered" else {}
    return ledger.record(
        PREREG_OP,
        rows_before=0,
        rows_after=0,
        params={"answers": answers, "answers_sha256": digest},
        **hashes,
    )


def check_answers(entries: list[dict[str, Any]], project_path: str | Path) -> list[str]:
    """Problems with the project answers against the run's freeze. Empty means they match."""
    if not entries or entries[0].get("op") != PREREG_OP:
        return ["the run has no preregistration entry; freeze the answers when the run starts"]
    frozen = entries[0]["params"]["answers"]
    if answers_digest(frozen) != entries[0]["params"]["answers_sha256"]:
        return ["the preregistration entry does not match its own hash"]
    current = _read_answers(project_path)
    changed = sorted(key for key in set(frozen) | set(current) if frozen.get(key) != current.get(key))
    if changed:
        return [
            f"answers changed since the run started: {', '.join(changed)}. "
            "Start a new run; do not edit answers for a run in progress."
        ]
    return []
