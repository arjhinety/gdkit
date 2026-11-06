"""Model and dataset cards generated from the run's ledger (Must 2).

A card is a JSON document. Every value comes from the ledger, the run's receipts, its paper lookups, or its
``model_setup.json``. Nothing is typed in by hand. ``audit_card`` regenerates a card from the run and
compares it with the file, so a hand edit or a card built from an older ledger shows up as a mismatch.

Dataset counts follow these ledger conventions (op names are the contract):
* the first entry's ``rows_after`` is the raw row count;
* ``validate`` entries remove rejected rows: their ``rows_before - rows_after`` is the rejected count;
* ``dedup`` entries remove duplicates: their ``rows_before - rows_after`` is the deduplicated count;
* the last entry's ``rows_after`` is the final row count;
* valid rows are raw minus rejected.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from gdkit.gates import RECEIPTS_DIR
from gdkit.ledger import verify
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
    data_entries = [e for e in entries if e["op"] != LOOKUP_OP]
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


def model_card(run_dir: str | Path) -> dict[str, Any]:
    run_dir = Path(run_dir)
    entries = _entries(run_dir)
    setup_path = run_dir / MODEL_SETUP_FILE
    if not setup_path.exists():
        raise CardError(f"{MODEL_SETUP_FILE} is missing")
    setup = json.loads(setup_path.read_text(encoding="utf-8"))
    missing = [field for field in MODEL_SETUP_FIELDS if not setup.get(field)]
    if missing:
        raise CardError(f"{MODEL_SETUP_FILE} lacks: {', '.join(missing)}")

    receipts = {}
    receipts_dir = run_dir / RECEIPTS_DIR
    if receipts_dir.exists():
        for path in sorted(receipts_dir.glob("*.json")):
            receipt = json.loads(path.read_text(encoding="utf-8"))
            receipts[receipt["gate"]] = receipt["receipt_hash"]

    lookups = [e["params"] for e in entries if e["op"] == LOOKUP_OP]
    return {
        "card": "model",
        "run_id": entries[0]["run_id"],
        "mode": entries[0]["mode"],
        "ledger_head": _head(entries),
        "ledger_entries": len(entries),
        **{field: setup[field] for field in MODEL_SETUP_FIELDS},
        "gate_receipts": receipts,
        "paper_lookups_ok": sum(1 for p in lookups if p.get("status") == "OK"),
        "paper_lookups_unavailable": sum(1 for p in lookups if p.get("status") == "UNAVAILABLE"),
    }


def write_card(path: str | Path, card: dict[str, Any]) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(card, sort_keys=True, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def audit_card(card_path: str | Path, regenerated: dict[str, Any]) -> list[str]:
    """Field names where the card on disk differs from a card regenerated from the run. Empty means it matches."""
    card_path = Path(card_path)
    on_disk = json.loads(card_path.read_text(encoding="utf-8"))
    keys = sorted(set(on_disk) | set(regenerated))
    return [key for key in keys if on_disk.get(key) != regenerated.get(key)]
