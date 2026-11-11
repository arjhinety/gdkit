"""Run recording: start a run, and record its side files in the ledger as they are written.

    python -m gdkit.run start  --runs-dir runs --run-id ID --mode explore|registered --project .gdkit/project.yaml
    python -m gdkit.run host   RUN_DIR [--package NAME ...] [--probe "COMMAND ARGS" ...]
    python -m gdkit.run scope  RUN_DIR --json FILE
    python -m gdkit.run config RUN_DIR --json FILE
    python -m gdkit.run decide RUN_DIR --json FILE
    python -m gdkit.run record RUN_DIR --op OP --file PATH_IN_RUN
    python -m gdkit.run audit  RUN_DIR [--registry module:ATTR] [--attestation-repo R --allowed-signers S]

Each command prints one JSON object with ``ok``; exit status 0 when ok.

Layout of ``runs/<run_id>/`` (beside ``ledger.jsonl``, ``receipts/`` and ``paper_lookups/``):

* ``run.json``: run ID, mode, gdkit version, label, and the project file and runs folder relative to the project
  root. Recorded as the ``run_open`` entry, right after the preregistration.
* ``host.json``: the full host record (see ``gdkit.host``). Git-ignored by the run's ``.gitignore``; the ledger
  carries its hash and a summary without hostname, username or paths.
* ``scope.json`` and ``config.json``: what the run covers, and the harness configuration. ``config.json`` holds the
  model card's setup fields and replaces ``model_setup.json``.
* ``decisions/NNNNNN.json``: one file per decision, named by its ledger position.
* ``logs/``: gate logs written by ``run_gate(capture_log=True)``. Git-ignored.

Every side file is written as exact bytes and recorded as an event entry with its path, sha256 and size. In
registered mode the hash is also the entry's content hash; explore mode keeps it in ``params``.

Order matters: a gate's receipt binds the ledger head, so anything recorded after a gate makes that receipt stale.
Record host, scope, config, data and decisions first, then run the gates.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import re
import shlex
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from gdkit import __version__
from gdkit.ledger import Ledger
from gdkit.prereg import _project_root, _relative_to_project_root, freeze_run

RUN_FILE = "run.json"
SCOPE_FILE = "scope.json"
CONFIG_FILE = "config.json"
DECISIONS_DIR = "decisions"
GITIGNORE = b"host.json\nlogs/\n"
_RUN_ID = re.compile(r"[A-Za-z0-9_-]+")


class RunError(ValueError):
    """The run cannot be started or a file cannot be recorded."""


def json_bytes(data: Any) -> bytes:
    """The bytes gdkit writes for a JSON side file: sorted keys, two-space indent, LF line ends, UTF-8."""
    return (json.dumps(data, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")


def _inside(run_dir: Path, rel: str) -> Path:
    path = (run_dir / rel).resolve()
    if run_dir.resolve() not in path.parents:
        raise RunError(f"{rel}: a recorded file must be inside the run folder")
    return path


def record_file(ledger: Ledger, run_dir: str | Path, op: str, rel: str, *, params: dict | None = None) -> dict:
    """Record a file already in the run folder: an event entry with its path, sha256 and size."""
    run_dir = Path(run_dir)
    data = _inside(run_dir, rel).read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    body = {**(params or {}), "path": Path(rel).as_posix(), "sha256": digest, "bytes": len(data)}
    hashes = {"input_hash": digest, "output_hash": digest} if ledger.mode == "registered" else {}
    return ledger.record_event(op, params=body, **hashes)


def write_and_record(ledger: Ledger, run_dir: str | Path, op: str, rel: str, data: Any, **extra: Any) -> dict:
    """Write ``data`` as JSON bytes at ``rel`` inside the run, then record it."""
    run_dir = Path(run_dir)
    path = _inside(run_dir, rel)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(json_bytes(data))
    return record_file(ledger, run_dir, op, rel, params=extra or None)


def record_scope(ledger: Ledger, run_dir: str | Path, scope: dict) -> dict:
    return write_and_record(ledger, run_dir, "scope", SCOPE_FILE, scope)


def record_config(ledger: Ledger, run_dir: str | Path, config: dict) -> dict:
    return write_and_record(ledger, run_dir, "config", CONFIG_FILE, config)


def record_decision(ledger: Ledger, run_dir: str | Path, decision: dict) -> dict:
    """Write ``decisions/<seq>.json``, named by the ledger position of the entry that records it."""
    return write_and_record(ledger, run_dir, "decision", f"{DECISIONS_DIR}/{ledger.next_seq:06d}.json", decision)


def open_ledger(run_dir: str | Path) -> Ledger:
    """The run's ledger, with the run ID and mode from its ``run.json``."""
    meta = json.loads((Path(run_dir) / RUN_FILE).read_text(encoding="utf-8"))
    return Ledger(Path(run_dir) / "ledger.jsonl", run_id=meta["run_id"], mode=meta["mode"])


def start_run(
    runs_dir: str | Path,
    run_id: str,
    *,
    mode: str,
    project_path: str | Path,
    questions_data: dict | None = None,
    label: str | None = None,
) -> Path:
    """Create ``runs_dir/run_id``, freeze the answers as entry 0, then write and record ``run.json``.

    The freeze comes before any side file, so a refused freeze (for example an undeclared difference from an
    explore run) leaves an empty folder that a retry can use.
    """
    if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
        raise RunError(f"run id {run_id!r} may use only letters, digits, '_' and '-'")
    runs_dir = Path(runs_dir)
    run_dir = runs_dir / run_id
    ledger_path = run_dir / "ledger.jsonl"
    if ledger_path.exists() and ledger_path.stat().st_size > 0:
        raise RunError(f"run {run_id} already started; use a new run id")
    run_dir.mkdir(parents=True, exist_ok=True)
    ledger = Ledger(ledger_path, run_id=run_id, mode=mode)
    freeze_run(ledger, project_path, questions_data=questions_data, runs_dir=runs_dir)
    (run_dir / ".gitignore").write_bytes(GITIGNORE)
    root = _project_root(project_path)
    meta = {
        "run_id": run_id,
        "mode": mode,
        "gdkit_version": __version__,
        "label": label,
        "project_file": Path(project_path).resolve().relative_to(root).as_posix()
        if root in Path(project_path).resolve().parents
        else Path(project_path).name,
        "runs_dir": _relative_to_project_root(runs_dir, project_path),
        "created": datetime.now(timezone.utc).isoformat(),
    }
    write_and_record(ledger, run_dir, "run_open", RUN_FILE, meta)
    return run_dir


def _load_registry(spec: str) -> dict:
    module_name, _, attribute = spec.partition(":")
    return getattr(importlib.import_module(module_name), attribute)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="gdkit run")
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("start")
    start.add_argument("--runs-dir", default="runs")
    start.add_argument("--run-id", required=True)
    start.add_argument("--mode", required=True, choices=("explore", "registered"))
    start.add_argument("--project", default=".gdkit/project.yaml")
    start.add_argument("--questions", help="validate the answers against this questions file before freezing")
    start.add_argument("--label")
    host = commands.add_parser("host")
    host.add_argument("run_dir")
    host.add_argument("--package", action="append", default=[])
    host.add_argument("--probe", action="append", default=[], help="a command whose output is kept in host.json")
    for name in ("scope", "config", "decide"):
        sub = commands.add_parser(name)
        sub.add_argument("run_dir")
        sub.add_argument("--json", required=True, help="a JSON file with the content to record")
    record = commands.add_parser("record")
    record.add_argument("run_dir")
    record.add_argument("--op", required=True)
    record.add_argument("--file", required=True, help="a path inside the run folder")
    audit = commands.add_parser("audit")
    audit.add_argument("run_dir")
    audit.add_argument("--registry")
    audit.add_argument("--attestation-repo")
    audit.add_argument("--allowed-signers")
    args = parser.parse_args(argv)

    try:
        out: dict[str, Any]
        if args.command == "start":
            questions = None
            if args.questions:
                from gdkit.interview import load_questions

                questions = load_questions(args.questions)
            run_dir = start_run(args.runs_dir, args.run_id, mode=args.mode, project_path=args.project,
                                questions_data=questions, label=args.label)
            out = {"ok": True, "run_dir": str(run_dir)}
        elif args.command == "audit":
            from gdkit.audit import verify_run

            report = verify_run(
                args.run_dir,
                registry=_load_registry(args.registry) if args.registry else None,
                attestation_repo=args.attestation_repo,
                allowed_signers=args.allowed_signers,
            )
            out = {"ok": not report["problems"], "report": report}
        else:
            ledger = open_ledger(args.run_dir)
            if args.command == "host":
                from gdkit.host import record_host

                entry = record_host(ledger, args.run_dir, packages=args.package,
                                    probes=[shlex.split(p) for p in args.probe])
            elif args.command == "record":
                entry = record_file(ledger, args.run_dir, args.op, args.file)
            else:
                content = json.loads(Path(args.json).read_text(encoding="utf-8"))
                writer = {"scope": record_scope, "config": record_config, "decide": record_decision}[args.command]
                entry = writer(ledger, args.run_dir, content)
            out = {"ok": True, "op": entry["op"], "seq": entry["seq"], "params": entry["params"]}
    except Exception as exc:  # one JSON answer, not a traceback
        out = {"ok": False, "message": f"{type(exc).__name__}: {exc}"}
    print(json.dumps(out, ensure_ascii=False))
    return 0 if out["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
