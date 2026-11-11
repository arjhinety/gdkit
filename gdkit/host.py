"""The host record: what the run actually executed on, read by machine, not typed in.

``record_host`` writes the full record to ``host.json`` in the run folder and appends a ``host`` event whose params
hold the file's sha256 and a summary. The full record has the details a reproduction needs and a stranger should not
see: hostname, username, interpreter path, working folder, and the raw output of each probe command (GPU UUIDs,
driver versions). The run's ``.gitignore`` keeps ``host.json`` out of git. The summary, which goes in the ledger,
has only:

* Python version and implementation; OS system, release and machine;
* package versions (``gdkit`` and ``pyyaml`` always, plus the ones the caller names), from ``importlib.metadata``;
* the project's git commit and whether the tree is dirty;
* for each probe: the program name, whether it ran, its return code, and the sha256 of its output.

Nothing here imports torch or any GPU library. A project that needs GPU details passes a probe such as
``["nvidia-smi", "--query-gpu=name,driver_version", "--format=csv"]``.
"""

from __future__ import annotations

import getpass
import hashlib
import platform
import subprocess
import sys
from importlib import metadata
from pathlib import Path
from typing import Any, Sequence

from gdkit.ledger import Ledger
from gdkit.run import json_bytes, record_file

HOST_FILE = "host.json"
ALWAYS = ("gdkit", "pyyaml")


def _probe(command: Sequence[str], timeout: int = 30) -> dict[str, Any]:
    try:
        proc = subprocess.run(list(command), capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"command": list(command), "available": False, "returncode": None, "stdout": "", "reason": str(exc)}
    return {
        "command": list(command),
        "available": proc.returncode == 0,
        "returncode": proc.returncode,
        "stdout": proc.stdout.strip(),
        "stderr": proc.stderr.strip()[:2000],
    }


def _git(cwd: Path) -> dict[str, Any]:
    head = _probe(["git", "-C", str(cwd), "rev-parse", "HEAD"])
    if not head["available"]:
        return {"commit": None, "dirty": None}
    status = _probe(["git", "-C", str(cwd), "status", "--porcelain"])
    return {"commit": head["stdout"], "dirty": bool(status["stdout"]) if status["available"] else None}


def _version(name: str) -> str | None:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return None


def collect_host(
    *, packages: Sequence[str] = (), probes: Sequence[Sequence[str]] = (), cwd: str | Path | None = None
) -> dict[str, Any]:
    """The full host record. Contains private details; see the module docstring."""
    cwd = Path(cwd) if cwd is not None else Path.cwd()
    try:
        user = getpass.getuser()
    except Exception:  # no login name in some containers
        user = None
    return {
        "python": {
            "version": platform.python_version(),
            "implementation": platform.python_implementation(),
            "executable": sys.executable,
        },
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "node": platform.node(),
        },
        "user": user,
        "cwd": str(cwd),
        "packages": {name: _version(name) for name in dict.fromkeys([*ALWAYS, *packages])},
        "git": _git(cwd),
        "probes": [_probe(command) for command in probes],
    }


def host_summary(full: dict[str, Any]) -> dict[str, Any]:
    """The part of the host record that goes in the ledger. No hostname, username, paths or raw output."""
    return {
        "python": {k: full["python"][k] for k in ("version", "implementation")},
        "platform": {k: full["platform"][k] for k in ("system", "release", "machine")},
        "packages": full["packages"],
        "git": full["git"],
        "probes": [
            {
                "name": Path(probe["command"][0]).name if probe["command"] else None,
                "available": probe["available"],
                "returncode": probe["returncode"],
                "stdout_sha256": hashlib.sha256(probe["stdout"].encode("utf-8")).hexdigest()
                if probe["available"]
                else None,
            }
            for probe in full["probes"]
        ],
    }


def record_host(
    ledger: Ledger,
    run_dir: str | Path,
    *,
    packages: Sequence[str] = (),
    probes: Sequence[Sequence[str]] = (),
    cwd: str | Path | None = None,
) -> dict[str, Any]:
    """Write ``host.json`` and append a ``host`` event with its hash and summary."""
    run_dir = Path(run_dir)
    full = collect_host(packages=packages, probes=probes, cwd=cwd)
    (run_dir / HOST_FILE).write_bytes(json_bytes(full))
    return record_file(ledger, run_dir, "host", HOST_FILE, params={"summary": host_summary(full)})
