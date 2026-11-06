"""Paper lookups through the Papers With Code CLI, recorded in the run's ledger (Must 11a, 11d).

Every call writes one ledger entry, ``op = "paper_lookup"``. Its ``params`` hold:

* the request (command and arguments), its sha256, and the call time (the entry's ``ts``);
* the ``pwc`` version and the response ``schema_version``;
* the papers returned, with their stable fields (paper ID, arXiv ID, title, publication date);
* the volatile fields (``citation_count``, ``has_official_implementation``, ``code_repository_count``),
  a closed list, each stored with the call time;
* the path of the raw JSON response under the run directory, and its sha256.

A failed or missing ``pwc`` writes an entry with ``status = "UNAVAILABLE"`` and the reason. Nothing is
silently skipped. Registered mode also gets the request and response hashes in the entry's content-hash
fields, because the ledger requires them there. Explore mode does not, because the ledger allows content
hashes only on boundary entries.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

from gdkit.ledger import Ledger
from gdkit.stream_hash import canonical_json_bytes

VOLATILE_FIELDS = ("citation_count", "has_official_implementation", "code_repository_count")
STABLE_FIELDS = ("id", "arxiv_id", "title", "published")
LOOKUP_OP = "paper_lookup"
LOOKUPS_DIR = "paper_lookups"


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _pwc_version(pwc_bin: list[str]) -> tuple[str | None, str | None]:
    """(pwc version, API version) from ``pwc version``, or (None, None) if that fails."""
    try:
        out = subprocess.run([*pwc_bin, "version"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None, None
    if out.returncode != 0:
        return None, None
    first = out.stdout.strip().splitlines()[0] if out.stdout.strip() else ""
    parts = first.split()
    cli = parts[1] if len(parts) > 1 else None
    api = parts[-1] if "api" in parts else None
    return cli, api


def _papers(payload: dict[str, Any]) -> list[dict[str, Any]]:
    data = payload.get("data", payload)
    if isinstance(data, dict) and "results" in data:
        return list(data["results"])
    return [data] if isinstance(data, dict) else []


def _record(
    ledger: Ledger,
    *,
    request_hash: str,
    response_hash: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    hashes: dict[str, str] = {}
    if ledger.mode == "registered":
        hashes = {"input_hash": request_hash, "output_hash": response_hash}
    params = {**params, "request_sha256": request_hash, "response_sha256": response_hash}
    return ledger.record(LOOKUP_OP, rows_before=0, rows_after=0, params=params, **hashes)


def lookup(
    ledger: Ledger,
    run_dir: str | Path,
    args: list[str],
    *,
    pwc_bin: list[str] | None = None,
    timeout: int = 120,
) -> dict[str, Any]:
    """Run ``pwc <args> --json``, record the call in the ledger, and return the recorded params."""
    run_dir = Path(run_dir)
    pwc_bin = pwc_bin or ["pwc"]
    command = [*pwc_bin, *args, "--json"]
    request_hash = _sha256_bytes(canonical_json_bytes({"command": command}))
    pwc_version, api_version = _pwc_version(pwc_bin)

    def unavailable(reason: str, raw: bytes = b"") -> dict[str, Any]:
        params = {
            "status": "UNAVAILABLE",
            "reason": reason,
            "command": command,
            "pwc_version": pwc_version,
            "api_version": api_version,
        }
        entry = _record(ledger, request_hash=request_hash, response_hash=_sha256_bytes(raw or reason.encode()), params=params)
        return entry["params"]

    try:
        proc = subprocess.run(command, capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return unavailable(f"pwc did not run: {exc}")
    if proc.returncode != 0:
        return unavailable(f"pwc exited with status {proc.returncode}: {proc.stderr.decode('utf-8', 'replace').strip()}")

    raw = proc.stdout
    try:
        payload = json.loads(raw)
    except ValueError:
        return unavailable("pwc returned a response that is not JSON", raw)

    lookups = run_dir / LOOKUPS_DIR
    lookups.mkdir(parents=True, exist_ok=True)
    seq = len(list(lookups.glob("*.json"))) + 1
    raw_path = lookups / f"{seq:04d}.json"
    raw_path.write_bytes(raw)
    response_hash = _sha256_bytes(raw)

    papers = _papers(payload)
    params = {
        "status": "OK",
        "command": command,
        "pwc_version": pwc_version,
        "api_version": api_version,
        "schema_version": payload.get("schema_version"),
        "raw_response_path": raw_path.relative_to(run_dir).as_posix(),
        "papers": [{field: paper.get(field) for field in STABLE_FIELDS} for paper in papers],
        "volatile": {
            str(paper.get("id")): {field: paper.get(field) for field in VOLATILE_FIELDS} for paper in papers
        },
    }
    return _record(ledger, request_hash=request_hash, response_hash=response_hash, params=params)["params"]
