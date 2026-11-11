"""The append-only check for committed run records, for a study repository's CI.

    python -m gdkit.append_only --base SHA [--head HEAD] [--repo .] [--runs-dir runs]
                                [--issues-log .gdkit/issues/log.jsonl]

Compares two commits (git blobs as bytes, never the working tree) and prints one JSON object with ``examined`` (how
many changed files the rules applied to) and ``violations``. Exit 0 when clean, 1 on a violation, 2 when the base
commit cannot be used (all zeros, as on a branch's first push, or missing because the clone is shallow).

Rules, for files under ``<runs-dir>/<run_id>/`` and for the project issue log:

* a run's ``ledger.jsonl`` and the issue log may only grow: the new bytes must start with the old bytes. Deleting
  either is a violation. Renames are not followed, so moving a ledger counts as deleting it;
* no run file may be deleted, and ``run.json`` may not change;
* any other run file (receipts, side files, committed logs) may be rewritten, because re-running a gate or
  re-recording a config is normal, but only if the record at the new commit accounts for it: a rewritten receipt
  must name a ``ledger_head`` that is an entry of the run's ledger, and any other rewritten file's new sha256 must
  appear in the run's ledger (an entry's ``sha256``, ``input_hash`` or ``output_hash``) or in one of its receipts
  (an input hash or the log hash). Line-ending conversion by git is allowed for in that comparison.

This only counts as enforcement when the branch is protected and the job is required. Without that, it reports.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import Any

ZERO = "0" * 40


class AppendOnlyError(ValueError):
    """The comparison cannot be made (bad base commit, not a git repository)."""


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True)


def _blob(repo: Path, rev: str, path: str) -> bytes | None:
    proc = _git(repo, "cat-file", "blob", f"{rev}:{path}")
    return proc.stdout if proc.returncode == 0 else None


def _commit(repo: Path, rev: str, label: str) -> str:
    if not rev or set(rev) == {"0"}:
        raise AppendOnlyError(f"{label} {rev!r} is not a commit (a first push has no previous commit); pass --base "
                              "with the commit to compare against, such as the merge base with the default branch")
    proc = _git(repo, "rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}")
    if proc.returncode != 0:
        raise AppendOnlyError(f"{label} {rev} is not in this clone; fetch full history (fetch-depth: 0)")
    return proc.stdout.decode().strip()


def _hashes(data: bytes) -> set[str]:
    """sha256 of the blob, and of its other line-ending form, since git may convert on checkout or commit."""
    variants = {data, data.replace(b"\r\n", b"\n"), data.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")}
    return {hashlib.sha256(v).hexdigest() for v in variants}


def _run_record(repo: Path, head: str, run_root: str) -> tuple[set[str], set[str]]:
    """(entry hashes of the run's ledger, file hashes the ledger and receipts account for) at ``head``."""
    entries, accounted = set(), set()
    ledger = _blob(repo, head, f"{run_root}/ledger.jsonl")
    for line in (ledger or b"").decode("utf-8", "replace").splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        entries.add(entry.get("entry_hash"))
        params = entry.get("params") or {}
        accounted.update(h for h in (params.get("sha256"), entry.get("input_hash"), entry.get("output_hash")) if h)
    listing = _git(repo, "ls-tree", "--name-only", f"{head}:{run_root}/receipts")
    for name in listing.stdout.decode().splitlines() if listing.returncode == 0 else []:
        try:
            receipt = json.loads(_blob(repo, head, f"{run_root}/receipts/{name}") or b"{}")
        except ValueError:
            continue
        accounted.update((receipt.get("inputs") or {}).values())
        if receipt.get("log"):
            accounted.add(receipt["log"].get("sha256"))
    return entries, accounted


def check(
    repo: str | Path,
    base: str,
    head: str = "HEAD",
    *,
    runs_dir: str = "runs",
    issues_log: str = ".gdkit/issues/log.jsonl",
) -> dict[str, Any]:
    """Apply the rules to every change between ``base`` and ``head``. See the module docstring."""
    repo = Path(repo)
    base, head = _commit(repo, base, "base"), _commit(repo, head, "head")
    # -z: NUL-separated and never quoted, so a path with non-ASCII or special characters is still examined.
    diff = _git(repo, "diff", "-z", "--no-renames", "--name-status", base, head)
    if diff.returncode != 0:
        raise AppendOnlyError(diff.stderr.decode().strip())
    fields = diff.stdout.decode("utf-8", "surrogateescape").split("\0")
    changes = list(zip(fields[0::2], fields[1::2]))
    runs = PurePosixPath(runs_dir).parts
    examined, violations = 0, []
    for status, path in changes:
        status, parts = status[:1], PurePosixPath(path).parts
        in_run = len(parts) >= len(runs) + 2 and parts[: len(runs)] == runs
        is_issue_log = path == issues_log
        if not (in_run or is_issue_log):
            continue
        examined += 1
        rest = parts[len(runs) + 1:] if in_run else ()
        is_ledger = is_issue_log or rest == ("ledger.jsonl",)
        if status == "A":
            continue
        if status == "D":
            violations.append(f"{path}: deleted; run records may not be deleted")
            continue
        if is_ledger:
            old, new = _blob(repo, base, path), _blob(repo, head, path)
            if old is None or new is None or not new.startswith(old):
                violations.append(f"{path}: not a byte-prefix extension of its previous state; a correction is a "
                                  "new entry, not an edit")
            continue
        if rest == ("run.json",):
            violations.append(f"{path}: run.json may not change after the run starts")
            continue
        run_root = "/".join(parts[: len(runs) + 1])
        entries, accounted = _run_record(repo, head, run_root)
        new = _blob(repo, head, path) or b""
        if rest[0] == "receipts" and path.endswith(".json"):
            try:
                ledger_head = json.loads(new).get("ledger_head")
            except (ValueError, AttributeError):
                ledger_head = None
            if ledger_head not in entries:
                violations.append(f"{path}: rewritten, but its ledger_head is not an entry of the run's ledger")
            continue
        if not (_hashes(new) & accounted):
            violations.append(f"{path}: rewritten, but no ledger entry or receipt at {head[:12]} records its new hash")
    return {"ok": not violations, "base": base, "head": head, "examined": examined, "violations": violations}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="gdkit append_only")
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--repo", default=".")
    parser.add_argument("--runs-dir", default="runs")
    parser.add_argument("--issues-log", default=".gdkit/issues/log.jsonl")
    args = parser.parse_args(argv)
    try:
        report = check(args.repo, args.base, args.head, runs_dir=args.runs_dir, issues_log=args.issues_log)
    except AppendOnlyError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 2
    print(json.dumps(report))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
