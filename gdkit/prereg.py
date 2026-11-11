"""Pre-registration freeze: the run's answers are recorded when it starts, and checked before it is promoted.

``freeze_run`` writes the run's first ledger entry, ``op = "preregistration"``, with the project answers
verbatim and their sha256, and with two declaration lists (empty when there is nothing to declare):
``replaced_unset``, questions undecided in explore mode and answered since, and ``changed_after_explore``,
real answers deliberately changed since. It refuses if any data entry already exists, because an answer fixed
after the first data action is not pre-registered.

For a registered run it also reads the sibling runs' explore-mode freezes and refuses any difference from
them that the two lists do not declare. Declared differences are frozen and shown on the model card, so a
reviewer can see which answers came after an explore run. The run IDs checked are recorded as
``explore_runs_checked``, and the folder searched as ``explore_runs_dir``, relative to the project root (the
folder holding ``.gdkit/``). An empty list with an unexpected folder means the search had nothing to compare.

``check_answers`` compares the project file against that entry. Any answer or declaration list that differs
is named, and the run is refused. A run with no preregistration entry is refused too.

This is detection, not prevention. Whoever can rewrite the ledger can also rewrite the entry. The git
history of the run and the attestation make a rewrite visible.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import yaml

from gdkit.interview import CHANGED_AFTER_EXPLORE_KEY, REPLACED_UNSET_KEY, UNSET, split_answers, validate
from gdkit.ledger import Ledger, LedgerError, _entry_hash, verify
from gdkit.stream_hash import canonical_json_bytes

PREREG_OP = "preregistration"
LEDGER_FILE = "ledger.jsonl"


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


def _explore_freezes(runs_dir: Path, own_ledger: Path) -> list[tuple[str, dict[str, Any]]]:
    """(run ID, frozen answers) for every explore-mode freeze in ``runs_dir/*/ledger.jsonl``, sorted by run ID.

    Only the first entry decides whether a ledger is an explore freeze. An explore-freeze ledger that does not
    verify, or a first entry that cannot be read or does not match its own hash, is refused rather than skipped,
    because skipping it would let a damaged or hand-edited explore ledger hide the answers it froze. A damaged
    ledger whose intact first entry is not an explore freeze (a crashed registered run) is not this check's
    concern and is skipped.
    """
    found = []
    for path in sorted(runs_dir.glob(f"*/{LEDGER_FILE}")):
        if path.resolve() == own_ledger.resolve():
            continue
        name = path.parent.name
        with open(path, encoding="utf-8") as handle:
            first_line = next((line for line in handle if line.strip()), None)
        if first_line is None:
            continue
        try:
            first = json.loads(first_line)
            readable = isinstance(first, dict) and first.get("entry_hash") == _entry_hash(first)
        except ValueError:
            readable = False
        if not readable:
            raise PreregError(f"cannot check earlier run {name}: the first entry of its ledger cannot be read")
        if first.get("op") != PREREG_OP or first.get("mode") != "explore":
            continue
        try:
            verify(path)
        except (LedgerError, OSError) as exc:
            raise PreregError(f"cannot check earlier explore run {name}: its ledger does not verify ({exc})") from exc
        found.append((name, first["params"]["answers"]))
    return found


def _project_root(project_path: str | Path) -> Path:
    """The folder that holds ``.gdkit/`` when the project file is ``.gdkit/project.yaml``, else the file's folder."""
    parent = Path(project_path).resolve().parent
    return parent.parent if parent.name == ".gdkit" else parent


def _relative_to_project_root(folder: Path, project_path: str | Path) -> str:
    """``folder`` relative to the project root, with ``/`` separators (``..`` when it is outside the root).

    Falls back to the absolute path when there is no relative path, such as another drive on Windows.
    """
    try:
        return Path(os.path.relpath(folder.resolve(), _project_root(project_path))).as_posix()
    except ValueError:
        return folder.resolve().as_posix()


def _undeclared(explore: dict[str, Any], current: dict[str, Any], declared: dict[str, list[str]]) -> list[str]:
    """Questions whose current answer differs from the explore freeze without the matching declaration."""
    problems = []
    for qid in sorted(set(explore) | set(current)):
        before, now = explore.get(qid), current.get(qid)
        if before == now or now == UNSET:
            continue
        if before is None or before == UNSET:
            if qid not in declared[REPLACED_UNSET_KEY] and qid not in declared[CHANGED_AFTER_EXPLORE_KEY]:
                problems.append(
                    f"{qid} (undecided in explore, now answered): "
                    f"python -m gdkit.skill_api declare --id {qid} --list {REPLACED_UNSET_KEY}"
                )
        elif qid not in declared[CHANGED_AFTER_EXPLORE_KEY]:
            problems.append(
                f"{qid} ({before!r} in explore, now {now!r}): "
                f"python -m gdkit.skill_api declare --id {qid} --list {CHANGED_AFTER_EXPLORE_KEY}"
            )
    return problems


def freeze_run(
    ledger: Ledger,
    project_path: str | Path,
    *,
    questions_data: dict | None = None,
    runs_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Record the project answers as the run's first ledger entry. Returns the stored entry.

    For a registered run, the answers are first compared with every explore-mode freeze among the sibling runs
    (``runs_dir``, by default the directory holding this run's directory, as in ``runs/<run_id>/ledger.jsonl``).
    Any difference must be declared: a question undecided in explore under ``replaced_unset``, a changed real
    answer under ``changed_after_explore`` (written by ``skill_api change``). An undeclared difference refuses
    the freeze. Explore runs whose ledgers were deleted, or that never froze their answers, cannot be seen.
    """
    if ledger.path.exists() and ledger.path.stat().st_size > 0:
        raise PreregError("the run already has ledger entries; the freeze must come before the first data action")
    data = _read_answers(project_path)
    if questions_data is not None:
        problems = validate(data, questions_data, ledger.mode)
        if problems:
            raise PreregError("the answers are not acceptable to freeze: " + "; ".join(problems))
    answers, declared = split_answers(data)

    checked: list[str] = []
    runs = Path(runs_dir) if runs_dir is not None else ledger.path.parent.parent
    if ledger.mode == "registered":
        for run_id, explore_answers in _explore_freezes(runs, ledger.path):
            problems = _undeclared(explore_answers, answers, declared)
            if problems:
                raise PreregError(
                    f"answers differ from explore run {run_id} without a declaration. Each one may have been "
                    "chosen after the explore run saw the data. If that is understood, declare it with the "
                    f"command shown, then freeze again: {'; '.join(problems)}"
                )
            checked.append(run_id)

    digest = answers_digest(answers)
    hashes = {"input_hash": digest, "output_hash": digest} if ledger.mode == "registered" else {}
    params = {
        "answers": answers,
        "answers_sha256": digest,
        REPLACED_UNSET_KEY: declared[REPLACED_UNSET_KEY],
        CHANGED_AFTER_EXPLORE_KEY: declared[CHANGED_AFTER_EXPLORE_KEY],
    }
    if ledger.mode == "registered":
        params["explore_runs_checked"] = checked
        params["explore_runs_dir"] = _relative_to_project_root(runs, project_path)
    return ledger.record(PREREG_OP, rows_before=0, rows_after=0, params=params, **hashes)


def check_answers(entries: list[dict[str, Any]], project_path: str | Path) -> list[str]:
    """Problems with the project answers against the run's freeze. Empty means they match."""
    if not entries or entries[0].get("op") != PREREG_OP:
        return ["the run has no preregistration entry; freeze the answers when the run starts"]
    frozen = entries[0]["params"]["answers"]
    if answers_digest(frozen) != entries[0]["params"]["answers_sha256"]:
        return ["the preregistration entry does not match its own hash"]
    current, current_declared = split_answers(_read_answers(project_path))
    changed = sorted(key for key in set(frozen) | set(current) if frozen.get(key) != current.get(key))
    for key in (REPLACED_UNSET_KEY, CHANGED_AFTER_EXPLORE_KEY):
        if entries[0]["params"].get(key, []) != current_declared[key]:
            changed.append(key)
    if changed:
        return [
            f"answers changed since the run started: {', '.join(changed)}. "
            "Start a new run; do not edit answers for a run in progress."
        ]
    return []
