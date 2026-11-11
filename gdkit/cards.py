"""Model and dataset cards generated from the run's ledger (Must 2).

A card is a JSON document. Every value comes from the ledger, the run's receipts, its paper lookups, or its
``config.json`` (recorded by ``gdkit.run``; it must still match its recorded hash). Runs without a ``config``
entry fall back to ``model_setup.json``. Nothing is typed in by hand. ``audit_card`` regenerates a card from the run and
compares it with the file, so a hand edit or a card built from an older ledger shows up as a mismatch.

Dataset counts follow these ledger conventions (op names are the contract). Paper lookups and the
preregistration entry are not data entries and are left out:
* the first data entry's ``rows_after`` is the raw row count;
* ``validate`` entries remove rejected rows: their ``rows_before - rows_after`` is the rejected count;
* ``dedup`` entries remove duplicates: their ``rows_before - rows_after`` is the deduplicated count;
* the last data entry's ``rows_after`` is the final row count;
* valid rows are raw minus rejected.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from gdkit.gates import RECEIPTS_DIR
from gdkit.ledger import verify
from gdkit.prereg import PREREG_OP
from gdkit.pwc_lookup import LOOKUP_OP

MODEL_SETUP_FILE = "model_setup.json"
MODEL_SETUP_FIELDS = (
    "base_model_revision",
    "tokenizer_sha256",
    "config_sha256",
    "eval_harness_sha256",
    "prompt_template_sha256",
)
CARDS_DIR = "cards"
# Bumped when card fields change, so audit_card can say "regenerate" instead of listing every new field.
CARD_SCHEMA = 2


class CardError(ValueError):
    """The run cannot produce the card: a field is missing or the ledger is not usable."""


def _entries(run_dir: Path) -> list[dict[str, Any]]:
    path = run_dir / "ledger.jsonl"
    try:
        verify(path)
    except (OSError, ValueError) as exc:
        raise CardError(f"ledger does not verify: {exc}") from exc
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _head(entries: list[dict[str, Any]]) -> str:
    return entries[-1]["entry_hash"]


def dataset_card(run_dir: str | Path) -> dict[str, Any]:
    run_dir = Path(run_dir)
    entries = _entries(run_dir)
    # Events (host, decisions, config...) are marked in the entry. Lookups and the preregistration predate
    # that mark and are recognised by op.
    data_entries = [e for e in entries if not e.get("event") and e["op"] not in (LOOKUP_OP, PREREG_OP)]
    if not data_entries:
        raise CardError("the ledger has no data entries")
    raw = data_entries[0]["rows_after"]
    rejected = sum(e["rows_before"] - e["rows_after"] for e in data_entries if e["op"] == "validate")
    deduplicated = sum(e["rows_before"] - e["rows_after"] for e in data_entries if e["op"] == "dedup")
    dropped: list[str] = []
    for entry in data_entries:
        for column in entry.get("columns_dropped") or []:
            if column not in dropped:
                dropped.append(column)
    return {
        "card": "dataset",
        "card_schema": CARD_SCHEMA,
        "run_id": entries[0]["run_id"],
        "mode": entries[0]["mode"],
        "ledger_head": _head(entries),
        "raw_rows": raw,
        "rejected_rows": rejected,
        "valid_rows": raw - rejected,
        "deduplicated_rows": deduplicated,
        "final_rows": data_entries[-1]["rows_after"],
        "columns_dropped": dropped,
    }


def model_card(run_dir: str | Path, *, issues_path: str | Path | None = None) -> dict[str, Any]:
    """The model card. With ``issues_path``, it lists the issues that affect the run and the issue log head;
    without it, both fields are None (not checked), which is different from an empty list."""
    run_dir = Path(run_dir)
    entries = _entries(run_dir)
    latest = _latest_events(entries)
    config = latest.get("config")
    if config is not None:
        # config.json, recorded by gdkit.run, replaces model_setup.json. It must still be the recorded file.
        setup_path = run_dir / config["path"]
        if not setup_path.exists() or hashlib.sha256(setup_path.read_bytes()).hexdigest() != config["sha256"]:
            raise CardError(f"{config['path']} has changed since it was recorded (or is missing)")
    else:
        setup_path = run_dir / MODEL_SETUP_FILE
        if not setup_path.exists():
            raise CardError(f"no config entry in the ledger and {MODEL_SETUP_FILE} is missing")
    setup = json.loads(setup_path.read_text(encoding="utf-8"))
    missing = [field for field in MODEL_SETUP_FIELDS if not setup.get(field)]
    if missing:
        raise CardError(f"{setup_path.name} lacks: {', '.join(missing)}")

    receipts = {}
    receipts_dir = run_dir / RECEIPTS_DIR
    if receipts_dir.exists():
        for path in sorted(receipts_dir.glob("*.json")):
            receipt = json.loads(path.read_text(encoding="utf-8"))
            receipts[receipt["gate"]] = receipt["receipt_hash"]

    lookups = [e["params"] for e in entries if e["op"] == LOOKUP_OP]
    # Questions answered only after an explore run, as frozen in the preregistration entry. None means the
    # run has no preregistration entry, which is different from an empty list.
    prereg = entries[0]["params"] if entries[0]["op"] == PREREG_OP else None
    replaced = prereg.get("replaced_unset", []) if prereg is not None else None
    changed = prereg.get("changed_after_explore", []) if prereg is not None else None
    checked = prereg.get("explore_runs_checked") if prereg is not None else None
    searched = prereg.get("explore_runs_dir") if prereg is not None else None
    return {
        "card": "model",
        "card_schema": CARD_SCHEMA,
        "run_id": entries[0]["run_id"],
        "mode": entries[0]["mode"],
        "ledger_head": _head(entries),
        "ledger_entries": len(entries),
        **{field: setup[field] for field in MODEL_SETUP_FIELDS},
        "gate_receipts": receipts,
        "paper_lookups_ok": sum(1 for p in lookups if p.get("status") == "OK"),
        "paper_lookups_unavailable": sum(1 for p in lookups if p.get("status") == "UNAVAILABLE"),
        "preregistration_replaced_unset": replaced,
        "preregistration_changed_after_explore": changed,
        "preregistration_explore_runs_checked": checked,
        "preregistration_explore_runs_dir": searched,
        "config_sha256_recorded": (latest.get("config") or {}).get("sha256"),
        "scope_sha256": (latest.get("scope") or {}).get("sha256"),
        "host_sha256": (latest.get("host") or {}).get("sha256"),
        "host_summary": (latest.get("host") or {}).get("summary"),
        "decisions": [e["params"]["path"] for e in entries if e.get("event") and e["op"] == "decision"],
        **_issue_fields(issues_path, entries[0]["run_id"], sorted(receipts)),
    }


def _issue_fields(issues_path: str | Path | None, run_id: str, gates: list[str]) -> dict[str, Any]:
    if issues_path is None:
        return {"issues_log_head": None, "issues": None}
    from gdkit.issues import IssueLog, affecting, blocking_reason

    path = Path(issues_path)
    if not path.exists():
        return {"issues_log_head": None, "issues": []}
    log = IssueLog(path)
    return {
        "issues_log_head": log.head,
        "issues": [
            {
                "slug": issue["slug"],
                "kind": issue["kind"],
                "severity": issue["severity"],
                "status": issue["status"],
                "resolution": issue["resolution"],
                "workaround": issue["workaround"],
                "issue_url": issue["issue_url"],
                "blocks": blocking_reason(issue),
            }
            for issue in affecting(log, run_id, gates)
        ],
    }


def _latest_events(entries: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """The params of the latest event entry per op (config, scope, host...)."""
    latest: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if entry.get("event"):
            latest[entry["op"]] = entry["params"]
    return latest


def write_card(path: str | Path, card: dict[str, Any]) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(card, sort_keys=True, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def audit_card(card_path: str | Path, regenerated: dict[str, Any]) -> list[str]:
    """Field names where the card on disk differs from a card regenerated from the run. Empty means it matches."""
    card_path = Path(card_path)
    on_disk = json.loads(card_path.read_text(encoding="utf-8"))
    if on_disk.get("card_schema") != regenerated.get("card_schema"):
        return [
            f"card_schema: the card on disk is schema {on_disk.get('card_schema')}, "
            f"current is {regenerated.get('card_schema')}; regenerate it"
        ]
    keys = sorted(set(on_disk) | set(regenerated))
    return [key for key in keys if on_disk.get(key) != regenerated.get(key)]
