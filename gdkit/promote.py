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

Receipt schemas. Each receipt is checked under the rules of the ``receipt_schema`` it was written with
(``RECEIPT_RULES``), checked right after its hash so the field cannot be edited without detection:

* schema 1 (current): the result needs ``passed: true``; the tag is ``attest/<run_id>/<gate>/<first 16 hex of the
  receipt hash>`` (one tag per receipt, so a re-run gate can be approved again and earlier approvals stay), its
  subject is exactly the receipt hash, and its body has exactly one ``issues-head: <64 hex | none>`` line: the
  project issue log's head when the approval was signed. ``promote`` requires that signed head to still be an entry
  of the issue log, so deleting or rewriting the issues a reviewer saw is caught; issues added later are judged by
  the normal blocking rule. Promotable.
* schema 0 (no field; receipts written by gdkit 8cf73ec or 9027e56): no pass flag; the tag is ``attest/<run_id>``
  with the hash in its message, as 8cf73ec signed it. 9027e56 already used per-gate tags, but no attestation was
  ever signed with it, so only the 8cf73ec tag form is recognised for schema 0. Verify-only: ``promote`` and ``attestable_hash`` refuse it. Otherwise a failing receipt
  written today without the field would get the older, laxer rules. ``gdkit.audit.verify_run`` reports on it.
* any other value: refused, as written by a newer gdkit or as not an integer.

If the receipt names a gate log, a changed log is refused and an absent one (logs are usually git-ignored, so a CI
checkout has none) is noted in the report.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Any, Callable

from dataclasses import dataclass

from gdkit.census import CensusError, check_result_census, validate_census_dict
from gdkit.gates import RECEIPT_SCHEMA, RECEIPTS_DIR, Gate, receipt_hash
from gdkit.issues import DEFAULT_LOG as DEFAULT_ISSUES_LOG
from gdkit.issues import IssueError, IssueLog, blocking_issues
from gdkit.ledger import LedgerError, verify
from gdkit.prereg import _project_root, check_answers
from gdkit.pwc_lookup import LOOKUP_OP
from gdkit.stream_hash import canonical_json_bytes

# (run_id, gate name, receipt_hash) -> bool, or {"ok": bool, "issues_head": str | None} when the verifier also read
# the issue log head the approval was signed with (``git_attestation`` does).
Attestation = Callable[[str, str, str], Any]
_ISSUES_HEAD = re.compile(r"issues-head: (none|[0-9a-f]{64})")


class PromoteError(Exception):
    """A required check failed. The message names the gate and the reason."""


@dataclass(frozen=True)
class ReceiptRules:
    """How a receipt of one schema is judged."""

    promotable: bool
    passed: Callable[[Any], bool | None]  # None: the schema records no pass flag
    tag: Callable[[str, str, str], str]  # (run_id, gate, receipt hash) -> tag name
    message_ok: Callable[[str, str], bool]  # (tag message, receipt hash) -> bool


RECEIPT_RULES: dict[int, ReceiptRules] = {
    0: ReceiptRules(
        promotable=False,
        passed=lambda result: None,
        tag=lambda run_id, gate, digest: f"attest/{run_id}",
        message_ok=lambda message, digest: digest in message,
    ),
    1: ReceiptRules(
        promotable=True,
        passed=lambda result: isinstance(result, dict) and result.get("passed") is True,
        tag=lambda run_id, gate, digest: f"attest/{run_id}/{gate}/{digest[:16]}",
        message_ok=lambda message, digest: message.strip() == digest,
    ),
}
assert RECEIPT_SCHEMA in RECEIPT_RULES


def receipt_schema(receipt: dict[str, Any]) -> int:
    """The receipt's schema: 0 when the field is absent. Raises ValueError for a value that is not a known integer."""
    if "receipt_schema" not in receipt:
        return 0
    value = receipt["receipt_schema"]
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"receipt_schema {value!r} is not an integer")
    if value not in RECEIPT_RULES:
        raise ValueError(f"receipt_schema {value} was written by a newer gdkit; upgrade gdkit to check it")
    return value


def log_state(run_dir: Path, receipt: dict[str, Any]) -> str | None:
    """None when the receipt names no log, else "match", "absent" or "changed"."""
    log = receipt.get("log")
    if not log:
        return None
    path = run_dir / log.get("path", "")
    if not path.is_file():
        return "absent"
    return "match" if _sha256(path) == log.get("sha256") else "changed"


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


def _require_signed_issues_head(
    name: str, signed: str, project_path: str | Path, issues_path: str | Path | None
) -> None:
    """The issue log head an approval was signed with must still be an entry of the issue log."""
    if signed == "none":
        return  # nothing was recorded when it was signed; anything recorded since is judged by the blocking rule
    path = Path(issues_path) if issues_path is not None else default_issues_path(project_path)
    if not path.exists():
        raise PromoteError(f"{name}: the approval was signed with issue log head {signed[:12]}, but the issue log "
                           f"{path} is gone")
    try:
        hashes = IssueLog(path).entry_hashes()
    except IssueError as exc:
        raise PromoteError(f"{name}: {exc}") from exc
    if signed not in hashes:
        raise PromoteError(f"{name}: the approval was signed with issue log head {signed[:12]}, which is not in the "
                           "current issue log; the issues the reviewer saw were rewritten. Re-attest the gate")


def default_issues_path(project_path: str | Path) -> Path:
    """The project's issue log: ``.gdkit/issues/log.jsonl`` under the project root."""
    return _project_root(project_path) / DEFAULT_ISSUES_LOG


def _require_no_blocking_issues(
    run_id: str, gates: list[str], project_path: str | Path, issues_path: str | Path | None
) -> str | None:
    """Shared by promote and attestable_hash. Returns the issue log head (None when there is no log)."""
    path = Path(issues_path) if issues_path is not None else default_issues_path(project_path)
    try:
        head, messages = blocking_issues(path, run_id, gates)
    except IssueError as exc:
        raise PromoteError(str(exc)) from exc
    if messages:
        raise PromoteError("; ".join(messages))
    return head


def _check_receipt(run_dir: Path, name: str, gate: Gate, state: dict[str, str]) -> dict[str, Any]:
    """Checks 1 to 5 from the module docstring, and the pass flag. Returns the receipt if they all pass."""
    path = run_dir / RECEIPTS_DIR / f"{name}.json"
    if not path.exists():
        raise PromoteError(f"{name}: no receipt")
    receipt = json.loads(path.read_text(encoding="utf-8"))

    if receipt.get("receipt_hash") != receipt_hash(receipt):
        raise PromoteError(f"{name}: receipt hash does not match its body (hand-written or edited)")
    try:
        schema = receipt_schema(receipt)
    except ValueError as exc:
        raise PromoteError(f"{name}: {exc}") from exc
    rules = RECEIPT_RULES[schema]
    if not rules.promotable:
        raise PromoteError(
            f"{name}: the receipt is schema {schema}, written before receipt versions; it can be checked with "
            "gdkit.audit.verify_run but not promoted. re-run the gate under current gdkit"
        )
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
    if log_state(run_dir, receipt) == "changed":
        raise PromoteError(f"{name}: log {receipt['log']['path']} has changed since the receipt was written")

    result = receipt.get("result")
    if not rules.passed(result):
        raise PromoteError(f"{name}: the gate did not pass (its receipt's result is {result!r})")
    try:
        check_result_census(result)
        if "census" in receipt:
            validate_census_dict(receipt["census"])
    except CensusError as exc:
        raise PromoteError(f"{name}: {exc}") from exc
    return receipt


def _log_note(run_dir: Path, receipt: dict[str, Any]) -> str:
    return "; log absent" if log_state(run_dir, receipt) == "absent" else ""


def promote(
    run_dir: str | Path,
    registry: dict[str, Gate],
    required: list[str],
    attestation: Attestation | None = None,
    *,
    project_path: str | Path,
    issues_path: str | Path | None = None,
) -> dict:
    """Return a report if every required gate passes. Raise PromoteError on the first failure.

    ``project_path`` is the project file whose answers must still match the run's pre-registration freeze.
    ``issues_path`` is the project's issue log (default ``.gdkit/issues/log.jsonl`` under the project root). An
    unresolved issue that affects this run or a required gate, and is blocking with no workaround or degrading and
    untriaged, refuses the run (see ``gdkit.issues``). The report names the issue log head, or None without a log.
    """
    run_dir = Path(run_dir)
    state = _ledger_state(run_dir / "ledger.jsonl")
    _require_frozen(state, project_path)
    issues_head = _require_no_blocking_issues(state["run_id"], list(required), project_path, issues_path)
    report: dict[str, str] = {}
    attestations: dict[str, dict[str, Any]] = {}
    for name in required:
        gate = registry.get(name)
        if gate is None:
            raise PromoteError(f"{name}: no such gate in the registry")
        receipt = _check_receipt(run_dir, name, gate, state)
        if gate.cheap:
            fresh = gate.check(run_dir)
            if canonical_json_bytes(fresh) != canonical_json_bytes(receipt["result"]):
                raise PromoteError(f"{name}: re-running the cheap gate does not reproduce the receipt's result")
            report[name] = "re-run and matched" + _log_note(run_dir, receipt)
        else:
            outcome = attestation(state["run_id"], name, receipt["receipt_hash"]) if attestation else False
            ok = outcome.get("ok") is True if isinstance(outcome, dict) else outcome is True
            if not ok:
                raise PromoteError(f"{name}: expensive gate has no valid attestation")
            signed = outcome.get("issues_head") if isinstance(outcome, dict) else None
            if signed is not None:
                _require_signed_issues_head(name, signed, project_path, issues_path)
            attestations[name] = {"issues_head_signed": signed, "issues_head_checked": signed is not None}
            report[name] = "attested" + _log_note(run_dir, receipt)
    return {"run_id": state["run_id"], "ledger_head": state["head"], "issues_log_head": issues_head, "gates": report,
            "attestations": attestations}


def attestable_hash(
    run_dir: str | Path,
    name: str,
    registry: dict[str, Gate],
    *,
    project_path: str | Path,
    issues_path: str | Path | None = None,
) -> str:
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
    _require_no_blocking_issues(state["run_id"], [name], project_path, issues_path)
    receipt = _check_receipt(run_dir, name, gate, state)
    return receipt["receipt_hash"]


def git_attestation(repo: str | Path, allowed_signers: str | Path, *, schema: int = RECEIPT_SCHEMA) -> Attestation:
    """Attestation via an SSH-signed git tag, named and matched under ``schema``'s rules.

    Schema 1 (the default): ``attest/<run_id>/<gate>/<receipt hash[:16]>``, subject exactly the receipt hash, and a
    body with exactly one ``issues-head: <64 hex | none>`` line, which the verifier returns for ``promote`` to check.
    Schema 0: ``attest/<run_id>``, message containing the hash; only ``verify_run`` uses it.
    The tag must verify against ``allowed_signers`` (an OpenSSH allowed-signers file), and the tagged commit
    must contain the run's ledger at ``runs/<run_id>/ledger.jsonl``. That ties the attestation to a committed
    run, not to a loose file on a laptop.
    """
    repo = str(repo)
    signers = str(allowed_signers)
    rules = RECEIPT_RULES[schema]
    # Schema 0 matched the whole message, schema 1 the subject line.
    message_format = "%(contents)" if schema == 0 else "%(contents:subject)"

    def verify_attestation(run_id: str, gate: str, digest: str) -> Any:
        tag = rules.tag(run_id, gate, digest)
        check = subprocess.run(
            ["git", "-C", repo, "-c", "gpg.format=ssh", "-c", f"gpg.ssh.allowedSignersFile={signers}", "tag", "-v", tag],
            capture_output=True,
            text=True,
        )
        if check.returncode != 0:
            return False
        message = subprocess.run(
            ["git", "-C", repo, "tag", "-l", f"--format={message_format}", tag],
            capture_output=True,
            text=True,
        ).stdout
        if not rules.message_ok(message, digest):
            return False
        signed_head = None
        if schema != 0:
            body = subprocess.run(
                ["git", "-C", repo, "tag", "-l", "--format=%(contents:body)", tag], capture_output=True, text=True
            ).stdout
            lines = [line.strip() for line in body.splitlines() if line.strip()]
            heads = [m.group(1) for m in (_ISSUES_HEAD.fullmatch(line) for line in lines) if m]
            if len(heads) != 1 or len(lines) != 1:
                return False
            signed_head = heads[0]
        commit = subprocess.run(
            ["git", "-C", repo, "rev-list", "-n", "1", tag], capture_output=True, text=True
        ).stdout.strip()
        in_commit = subprocess.run(
            ["git", "-C", repo, "cat-file", "-e", f"{commit}:runs/{run_id}/ledger.jsonl"],
            capture_output=True,
            text=True,
        )
        if in_commit.returncode != 0:
            return False
        return True if schema == 0 else {"ok": True, "issues_head": signed_head}

    return verify_attestation
