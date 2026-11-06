"""Gate registry and the gate runner's receipt writer (guard layer a).

A receipt is a JSON file written by ``run_gate``, the only function in gdkit that writes receipts.
Each receipt binds the gate's name, its code hash, the run ID, the ledger head at the time of the
check, and the sha256 of every input file the gate read. ``promote`` checks all of these again.

Receipts are unsigned. Their ``receipt_hash`` is a sha256 of the body, so an accidental edit is
caught. It does not stop someone who recomputes the hash. The promote step is what refuses forgery.
"""

from __future__ import annotations

import hashlib
import inspect
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from gdkit.stream_hash import canonical_json_bytes

RECEIPTS_DIR = "receipts"


@dataclass(frozen=True)
class Gate:
    """A named check. ``cheap`` gates are re-run at promote time; expensive gates need an attestation."""

    name: str
    cheap: bool
    check: Callable[[Path], Any]

    @property
    def code_hash(self) -> str:
        """sha256 of the check's source text. A change to the gate changes this hash."""
        return hashlib.sha256(inspect.getsource(self.check).encode("utf-8")).hexdigest()


def receipt_hash(body: dict[str, Any]) -> str:
    """sha256 of a receipt body, excluding any ``receipt_hash`` field."""
    return hashlib.sha256(canonical_json_bytes({k: v for k, v in body.items() if k != "receipt_hash"})).hexdigest()


def sha256_path(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def run_gate(
    run_dir: str | Path,
    gate: Gate,
    *,
    inputs: list[str],
    run_id: str,
    ledger_head: str,
) -> Path:
    """Run ``gate`` on ``run_dir`` and write its receipt. Returns the receipt path.

    ``inputs`` are paths relative to ``run_dir``, the files the gate reads. Their hashes are taken
    before the check runs, so the receipt records what the check actually saw.
    """
    run_dir = Path(run_dir)
    input_hashes = {rel: sha256_path(run_dir / rel) for rel in inputs}
    result = gate.check(run_dir)
    body = {
        "run_id": run_id,
        "gate": gate.name,
        "cheap": gate.cheap,
        "gate_code_hash": gate.code_hash,
        "inputs": input_hashes,
        "result": result,
        "ledger_head": ledger_head,
    }
    body["receipt_hash"] = receipt_hash(body)
    out_dir = run_dir / RECEIPTS_DIR
    out_dir.mkdir(exist_ok=True)
    path = out_dir / f"{gate.name}.json"
    path.write_text(json.dumps(body, sort_keys=True, indent=2), encoding="utf-8")
    return path
