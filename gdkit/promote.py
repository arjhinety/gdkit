"""``promote()``: refuse a run unless every required receipt checks out (guard layers b and c).

Checks, in order, for each required gate:
1. The receipt exists, and its ``receipt_hash`` matches its body (catches hand edits).
2. Its ``run_id`` matches the run's ledger (catches receipts copied from another run).
3. Its ``ledger_head`` is the run's current ledger head (catches receipts from before a later change).
4. Its gate name and cheap flag match the registry, and its ``gate_code_hash`` matches the current gate.
5. Every input file it names still has the recorded sha256 (catches altered artifacts).
   Then its result must say ``passed: true`` (a receipt of a failed check is refused).
6. Cheap gates are re-run and must reproduce the receipt's result.
   Expensive gates are not re-run. They need an attestation that the caller's verifier accepts.

The ledger itself must verify, and every entry must be in registered mode (catches explore runs).
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any, Callable

from gdkit.gates import RECEIPTS_DIR, Gate, receipt_hash
from gdkit.ledger import LedgerError, verify
from gdkit.prereg import check_answers
from gdkit.pwc_lookup import LOOKUP_OP
from gdkit.stream_hash import canonical_json_bytes

Attestation = Callable[[str, str, str], bool]  # (run_id, gate name, receipt_hash) -> bool


class PromoteError(Exception):
    """A required check failed. The message names the gate and the reason."""


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _ledger_state(ledger_path: Path) -> dict[str, str]:
    try:
        verify(ledger_path)
    except (LedgerError, OSError) as exc:
        raise PromoteError(f"ledger does not verify: {exc}") from exc
    entries = [json.loads(line) for line in ledger_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not entries:
        raise PromoteError("ledger is empty")
    if any(entry["mode"] != "registered" for entry in entries):
        raise PromoteError("the run has explore entries and cannot be promoted; re-run it in registered mode")
    run_ids = {entry["run_id"] for entry in entries}
    if len(run_ids) != 1:
        raise PromoteError("ledger mixes several run IDs")
    # A paper lookup counts by its latest entry for the same request. The ledger is append-only, so a
    # later successful retry supersedes an earlier UNAVAILABLE entry and clears the block.
    latest: dict[str, dict] = {}
    for entry in entries:
        if entry.get("op") == LOOKUP_OP:
            latest[entry["params"]["request_sha256"]] = entry["params"]
    for params in latest.values():
        if params.get("status") == "UNAVAILABLE":
            command = " ".join(params.get("command", []))
            raise PromoteError(
                f"paper lookup still UNAVAILABLE ({command}): {params.get('reason')}. Retry it, then promote again"
            )
    return {"run_id": run_ids.pop(), "head": entries[-1]["entry_hash"], "entries": entries}


def _require_frozen(state: dict, project_path: str | Path) -> None:
    problems = check_answers(state["entries"], project_path)
    if problems:
        raise PromoteError(problems[0])


def _check_receipt(run_dir: Path, name: str, gate: Gate, state: dict[str, str]) -> dict[str, Any]:
    """Checks 1 to 5 from the module docstring, and the pass flag. Returns the receipt if they all pass."""
    path = run_dir / RECEIPTS_DIR / f"{name}.json"
    if not path.exists():
        raise PromoteError(f"{name}: no receipt")
    receipt = json.loads(path.read_text(encoding="utf-8"))

    if receipt.get("receipt_hash") != receipt_hash(receipt):
        raise PromoteError(f"{name}: receipt hash does not match its body (hand-written or edited)")
    if receipt.get("run_id") != state["run_id"]:
        raise PromoteError(f"{name}: receipt is from another run")
    if receipt.get("ledger_head") != state["head"]:
        raise PromoteError(f"{name}: receipt is stale, the ledger changed after it was written")
    if receipt.get("gate") != name or receipt.get("cheap") != gate.cheap:
        raise PromoteError(f"{name}: receipt does not match the registered gate")
    if receipt.get("gate_code_hash") != gate.code_hash:
        raise PromoteError(f"{name}: receipt was written by a different version of the gate code")

    for rel, digest in receipt.get("inputs", {}).items():
        artifact = run_dir / rel
        if not artifact.exists() or _sha256(artifact) != digest:
            raise PromoteError(f"{name}: input {rel} has changed since the receipt was written")

    result = receipt.get("result")
    if not isinstance(result, dict) or result.get("passed") is not True:
        raise PromoteError(f"{name}: the gate did not pass (its receipt's result is {result!r})")
    return receipt


def promote(
    run_dir: str | Path,
    registry: dict[str, Gate],
    required: list[str],
    attestation: Attestation | None = None,
    *,
    project_path: str | Path,
) -> dict:
    """Return a report if every required gate passes. Raise PromoteError on the first failure.

    ``project_path`` is the project file whose answers must still match the run's pre-registration freeze.
    """
    run_dir = Path(run_dir)
    state = _ledger_state(run_dir / "ledger.jsonl")
    _require_frozen(state, project_path)
    report: dict[str, str] = {}
    for name in required:
        gate = registry.get(name)
        if gate is None:
            raise PromoteError(f"{name}: no such gate in the registry")
        receipt = _check_receipt(run_dir, name, gate, state)
        if gate.cheap:
            fresh = gate.check(run_dir)
            if canonical_json_bytes(fresh) != canonical_json_bytes(receipt["result"]):
                raise PromoteError(f"{name}: re-running the cheap gate does not reproduce the receipt's result")
            report[name] = "re-run and matched"
        else:
            if attestation is None or not attestation(state["run_id"], name, receipt["receipt_hash"]):
                raise PromoteError(f"{name}: expensive gate has no valid attestation")
            report[name] = "attested"
    return {"run_id": state["run_id"], "ledger_head": state["head"], "gates": report}


def attestable_hash(run_dir: str | Path, name: str, registry: dict[str, Gate], *, project_path: str | Path) -> str:
    """Checks 1 to 5 and the pass flag for one expensive gate, then its receipt hash, for the attestation tag.

    This does not check the attestation itself, and it does not re-run the gate. The CI job
    that calls it is the only place a signing key is available.
    """
    run_dir = Path(run_dir)
    gate = registry.get(name)
    if gate is None:
        raise PromoteError(f"{name}: no such gate in the registry")
    if gate.cheap:
        raise PromoteError(f"{name}: cheap gates are re-run at promote time and are not attested")
    state = _ledger_state(run_dir / "ledger.jsonl")
    _require_frozen(state, project_path)
    receipt = _check_receipt(run_dir, name, gate, state)
    return receipt["receipt_hash"]


def git_attestation(repo: str | Path, allowed_signers: str | Path) -> Attestation:
    """Attestation via an SSH-signed git tag ``attest/<run_id>/<gate>``, one tag per expensive gate.

    The tag must verify against ``allowed_signers`` (an OpenSSH allowed-signers file). Its message
    must be exactly the receipt hash, and the tagged commit must contain the run's ledger at
    ``runs/<run_id>/ledger.jsonl``. That ties the attestation to a committed run, not to a
    loose file on a laptop.
    """
    repo = str(repo)
    signers = str(allowed_signers)

    def verify_attestation(run_id: str, gate: str, digest: str) -> bool:
        tag = f"attest/{run_id}/{gate}"
        check = subprocess.run(
            ["git", "-C", repo, "-c", "gpg.format=ssh", "-c", f"gpg.ssh.allowedSignersFile={signers}", "tag", "-v", tag],
            capture_output=True,
            text=True,
        )
        if check.returncode != 0:
            return False
        message = subprocess.run(
            ["git", "-C", repo, "tag", "-l", "--format=%(contents:subject)", tag],
            capture_output=True,
            text=True,
        ).stdout
        if message.strip() != digest:
            return False
        commit = subprocess.run(
            ["git", "-C", repo, "rev-list", "-n", "1", tag], capture_output=True, text=True
        ).stdout.strip()
        in_commit = subprocess.run(
            ["git", "-C", repo, "cat-file", "-e", f"{commit}:runs/{run_id}/ledger.jsonl"],
            capture_output=True,
            text=True,
        )
        return in_commit.returncode == 0

    return verify_attestation
