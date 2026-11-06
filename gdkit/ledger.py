"""Write-through provenance ledger (design doc, Must item 1).

Every data action is appended to a JSON-lines file as it happens, with an fsync, and each entry
carries the hash of the one before it. Modes follow the design doc:

* ``registered``: every entry must carry an input and an output content hash.
* ``explore``: content hashes are allowed only on boundary entries (raw sources and final splits),
  and boundary entries must carry both. Other entries record row counts, column diffs and parameters.

The chain detects accidental change to the file. It is not tamper proof: whoever can write the file
can rewrite the whole chain. Tamper resistance is the self-forgery guard's job, not this module's.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from gdkit.stream_hash import canonical_json_bytes

MODES = ("explore", "registered")
GENESIS = "GENESIS"


class LedgerError(ValueError):
    """Raised when an entry breaks the mode rules or the chain does not verify."""


def _entry_hash(entry: dict[str, Any]) -> str:
    body = {key: value for key, value in entry.items() if key != "entry_hash"}
    return hashlib.sha256(canonical_json_bytes(body)).hexdigest()


def verify(path: str | os.PathLike[str]) -> int:
    """Check every entry's hash and the chain order. Returns the entry count.

    Raises LedgerError naming the first bad line.
    """
    prev = GENESIS
    count = 0
    with open(path, encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            entry = json.loads(line)
            if entry.get("seq") != count:
                raise LedgerError(f"line {line_no}: seq {entry.get('seq')} expected {count}")
            if entry.get("prev_hash") != prev:
                raise LedgerError(f"line {line_no}: prev_hash does not match the previous entry")
            if entry.get("entry_hash") != _entry_hash(entry):
                raise LedgerError(f"line {line_no}: entry_hash does not match the entry content")
            prev = entry["entry_hash"]
            count += 1
    return count


class Ledger:
    """Append-only ledger for one run."""

    def __init__(self, path: str | os.PathLike[str], run_id: str, mode: str) -> None:
        if mode not in MODES:
            raise LedgerError(f"mode must be one of {MODES}, got {mode!r}")
        self.path = Path(path)
        self.run_id = run_id
        self.mode = mode
        if self.path.exists() and self.path.stat().st_size > 0:
            verify(self.path)
            self._seq, self._prev = self._tail()
        else:
            self._seq, self._prev = 0, GENESIS

    def _tail(self) -> tuple[int, str]:
        last = None
        with open(self.path, encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    last = json.loads(line)
        return (last["seq"] + 1, last["entry_hash"]) if last else (0, GENESIS)

    def record(
        self,
        op: str,
        *,
        rows_before: int,
        rows_after: int,
        params: dict[str, Any] | None = None,
        columns_dropped: list[str] | None = None,
        input_hash: str | None = None,
        output_hash: str | None = None,
        boundary: bool = False,
    ) -> dict[str, Any]:
        """Append one entry, enforcing the mode rules. Returns the stored entry."""
        has_hashes = input_hash is not None or output_hash is not None
        both_hashes = input_hash is not None and output_hash is not None
        if self.mode == "registered":
            if not both_hashes:
                raise LedgerError("registered mode: every entry needs an input and an output content hash")
        elif boundary:
            if not both_hashes:
                raise LedgerError("explore mode: boundary entries need an input and an output content hash")
        elif has_hashes:
            raise LedgerError("explore mode: content hashes are allowed only on boundary entries")

        entry: dict[str, Any] = {
            "run_id": self.run_id,
            "mode": self.mode,
            "seq": self._seq,
            "ts": datetime.now(timezone.utc).isoformat(),
            "op": op,
            "params": params or {},
            "rows_before": rows_before,
            "rows_after": rows_after,
            "columns_dropped": columns_dropped,
            "input_hash": input_hash,
            "output_hash": output_hash,
            "boundary": boundary,
            "prev_hash": self._prev,
        }
        entry["entry_hash"] = _entry_hash(entry)
        line = json.dumps(entry, sort_keys=True, separators=(",", ":"), allow_nan=False)
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        self._seq += 1
        self._prev = entry["entry_hash"]
        return entry
