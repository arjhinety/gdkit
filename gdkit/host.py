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
import json
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


class HostError(ValueError):
    """This machine is not the one recorded for the run, or the host record cannot be checked."""


def _latest_host_entry(run_dir: Path) -> dict[str, Any] | None:
    ledger = run_dir / "ledger.jsonl"
    if not ledger.is_file():
        return None
    latest = None
    for line in ledger.read_text(encoding="utf-8").splitlines():
        if line.strip():
            entry = json.loads(line)
            if entry.get("event") and entry.get("op") == "host":
                latest = entry
    return latest


def _differences(recorded: Any, current: Any, prefix: str = "") -> list[str]:
    if isinstance(recorded, dict) and isinstance(current, dict):
        out = []
        for key in sorted(set(recorded) | set(current)):
            out += _differences(recorded.get(key), current.get(key), f"{prefix}.{key}" if prefix else key)
        return out
    if isinstance(recorded, list) and isinstance(current, list) and len(recorded) == len(current):
        out = []
        for index, (a, b) in enumerate(zip(recorded, current)):
            out += _differences(a, b, f"{prefix}[{index}]")
        return out
    return [] if recorded == current else [f"{prefix}: recorded {recorded!r}, now {current!r}"]


def require_same_host(run_dir: str | Path) -> dict[str, Any]:
    """Refuse (HostError) unless this machine matches the run's latest recorded host.

    Returns what the receipt should say about the check: ``{"checked": True, "host_sha256": ...}``, or
    ``{"checked": False, "reason": ...}`` when the run has no host entry. The comparison re-collects the host with
    the same packages and probe commands (read from ``host.json``, which must still match its recorded hash) and
    compares every summary field except the git commit and dirty flag, which change as the run writes files.
    """
    run_dir = Path(run_dir)
    entry = _latest_host_entry(run_dir)
    if entry is None:
        return {"checked": False, "reason": "the run has no host entry"}
    params = entry["params"]
    remedy = (f"if this machine is meant to run the gate, re-record it with python -m gdkit.run host "
              f"{run_dir.as_posix()} (it reuses the packages and probes in host.json); re-recording adds a ledger "
              "entry, so receipts written before it become stale")
    host_file = run_dir / params["path"]
    if not host_file.is_file():
        summary = params.get("summary") or {}
        packages = " ".join(f"--package {name}" for name in summary.get("packages", {}) if name not in ALWAYS)
        probes = ", ".join(p.get("name") or "?" for p in summary.get("probes", []))
        raise HostError(
            f"{params['path']} is missing on this machine, so it is not the recorded host. If this machine is meant "
            f"to run the gate, re-record it with the same packages and probes as before: python -m gdkit.run host "
            f"{run_dir.as_posix()} {packages} --probe \"...\" for each recorded probe ({probes or 'none'}); "
            "re-recording adds a ledger entry, so receipts written before it become stale"
        )
    if hashlib.sha256(host_file.read_bytes()).hexdigest() != params["sha256"]:
        raise HostError(f"{params['path']} does not match its recorded hash; {remedy}")
    full = json.loads(host_file.read_text(encoding="utf-8"))
    now = host_summary(collect_host(packages=list(full["packages"]),
                                    probes=[probe["command"] for probe in full["probes"]]))
    recorded = {k: v for k, v in params["summary"].items() if k != "git"}
    current = {k: v for k, v in now.items() if k != "git"}
    differences = _differences(recorded, current)
    if differences:
        raise HostError("this machine differs from the recorded host: " + "; ".join(differences) + f"; {remedy}")
    return {"checked": True, "host_sha256": params["sha256"]}


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
