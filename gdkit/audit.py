"""``verify_run``: a read-only report on a run's ledger and receipts, under each receipt's own schema rules.

``promote`` answers "may this run be promoted now?". ``verify_run`` answers "what does this run's record still
show?", which is the question for runs recorded by an older gdkit. It writes nothing. For each receipt it reports:

* ``hash_ok`` and ``schema`` (0 when the receipt has no ``receipt_schema``);
* ``run_id_ok``, and ``ledger_head_seq``: which ledger entry the receipt was written against. ``current`` says
  whether that is still the head. A stale receipt is a fact about the run, not a broken receipt;
* ``inputs`` and ``log``: ``match``, ``changed`` or ``absent``. Absent files (git-ignored data, logs) are not
  problems; changed ones are;
* ``code``: whether the registry's gate code still has the recorded hash, when a registry is given;
* ``passed``: the schema's pass rule (``None`` for schema 0, which recorded no pass flag), and ``promotable``;
* ``attestation`` for expensive gates, checked under the receipt's schema when a repo and signers are given.

``problems`` lists what is actually broken: a hash that does not match, another run's receipt, a head that is not in
the chain, a changed input or log, an unknown schema, or a ledger that does not verify.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from gdkit.gates import RECEIPTS_DIR, Gate, receipt_hash, sha256_path
from gdkit.ledger import LedgerError, verify
from gdkit.promote import RECEIPT_RULES, git_attestation, log_state, receipt_schema


def _ledger(run_dir: Path, problems: list[str]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    path = run_dir / "ledger.jsonl"
    try:
        count = verify(path)
    except (LedgerError, OSError) as exc:
        problems.append(f"ledger does not verify: {exc}")
        return {"verifies": False, "error": str(exc)}, []
    entries = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    head = entries[-1]["entry_hash"] if entries else None
    return {"verifies": True, "entries": count, "head": head}, entries


def verify_run(
    run_dir: str | Path,
    *,
    registry: dict[str, Gate] | None = None,
    attestation_repo: str | Path | None = None,
    allowed_signers: str | Path | None = None,
) -> dict[str, Any]:
    """Report on every receipt in ``run_dir/receipts``. Read-only. See the module docstring for the fields."""
    run_dir = Path(run_dir)
    problems: list[str] = []
    ledger, entries = _ledger(run_dir, problems)
    run_id = entries[0]["run_id"] if entries else None
    seq_of = {entry["entry_hash"]: entry["seq"] for entry in entries}
    receipts: dict[str, dict[str, Any]] = {}

    receipts_dir = run_dir / RECEIPTS_DIR
    for path in sorted(receipts_dir.glob("*.json")) if receipts_dir.is_dir() else []:
        name = path.stem
        try:
            receipt = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:
            receipts[name] = {"readable": False}
            problems.append(f"{name}: receipt is not readable JSON ({exc})")
            continue
        if not isinstance(receipt, dict):
            receipts[name] = {"readable": False}
            problems.append(f"{name}: receipt is not a JSON object")
            continue
        item: dict[str, Any] = {"readable": True, "gate": receipt.get("gate"), "cheap": receipt.get("cheap")}
        receipts[name] = item

        item["hash_ok"] = receipt.get("receipt_hash") == receipt_hash(receipt)
        if not item["hash_ok"]:
            problems.append(f"{name}: receipt hash does not match its body")
        if receipt.get("gate") != name:
            problems.append(f"{name}: the receipt names gate {receipt.get('gate')!r}")
        try:
            schema = receipt_schema(receipt)
        except ValueError as exc:
            item["schema"] = receipt.get("receipt_schema")
            problems.append(f"{name}: {exc}")
            continue
        rules = RECEIPT_RULES[schema]
        item["schema"] = schema

        item["run_id_ok"] = run_id is not None and receipt.get("run_id") == run_id
        if run_id is not None and not item["run_id_ok"]:
            problems.append(f"{name}: receipt is from another run ({receipt.get('run_id')})")
        item["ledger_head_seq"] = seq_of.get(receipt.get("ledger_head"))
        item["current"] = ledger.get("head") is not None and receipt.get("ledger_head") == ledger.get("head")
        if entries and item["ledger_head_seq"] is None:
            problems.append(f"{name}: ledger_head is not an entry of this ledger")

        inputs = {}
        for rel, digest in receipt.get("inputs", {}).items():
            file = run_dir / rel
            inputs[rel] = "absent" if not file.is_file() else ("match" if sha256_path(file) == digest else "changed")
            if inputs[rel] == "changed":
                problems.append(f"{name}: input {rel} changed")
        item["inputs"] = inputs
        item["log"] = log_state(run_dir, receipt)
        if item["log"] == "changed":
            problems.append(f"{name}: log {receipt['log']['path']} changed")

        if registry is None:
            item["code"] = "not checked"
        elif name not in registry:
            item["code"] = "unknown gate"
        else:
            item["code"] = "match" if registry[name].code_hash == receipt.get("gate_code_hash") else "changed"

        item["result"] = receipt.get("result")
        item["passed"] = rules.passed(receipt.get("result"))
        item["promotable"] = rules.promotable

        if receipt.get("cheap"):
            item["attestation"] = "not needed (cheap gate)"
        elif attestation_repo is None or allowed_signers is None:
            item["attestation"] = "not checked"
        else:
            verifier = git_attestation(attestation_repo, allowed_signers, schema=schema)
            ok = run_id is not None and verifier(run_id, name, receipt.get("receipt_hash", ""))
            item["attestation"] = "verified" if ok else "not verified"

    return {"run_id": run_id, "ledger": ledger, "receipts": receipts, "problems": problems}
