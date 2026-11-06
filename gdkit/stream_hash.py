"""Content hashes for record streams, per the design doc's byte rules.

Each record is serialised as canonical JSON: UTF-8, object keys sorted, separators ``,`` and ``:``,
no whitespace, floats written by Python's ``repr``, NaN and Infinity rejected. Each record's digest
is the sha256 of its canonical bytes. The stream digest is the sha256 of the record digests in stream
order, each followed by LF. The digest does not depend on how the stream is chunked.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_json_bytes(record: Any) -> bytes:
    """Canonical JSON bytes for one record. Raises ValueError on NaN or Infinity."""
    text = json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return text.encode("utf-8")


def record_digest(record: Any) -> str:
    """Hex sha256 of one record's canonical bytes."""
    return hashlib.sha256(canonical_json_bytes(record)).hexdigest()


class StreamHasher:
    """Incremental stream digest. Feed records in stream order with ``add``."""

    def __init__(self) -> None:
        self._outer = hashlib.sha256()
        self.count = 0

    def add(self, record: Any) -> None:
        self._outer.update((record_digest(record) + "\n").encode("ascii"))
        self.count += 1

    def hexdigest(self) -> str:
        return self._outer.hexdigest()


def stream_digest(records: Any) -> str:
    """Convenience: the stream digest of an iterable of records."""
    hasher = StreamHasher()
    for record in records:
        hasher.add(record)
    return hasher.hexdigest()
