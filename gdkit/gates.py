"""Gate registry and the gate runner's receipt writer (guard layer a).

A receipt is a JSON file written by ``run_gate``, the only function in gdkit that writes receipts.
Each receipt binds the gate's name, its code hash, the run ID, the ledger head at the time of the
check, and the sha256 of every input file the gate read. ``promote`` checks all of these again.

A gate's check returns a JSON object with a boolean ``passed`` field, for example
``{"passed": True, "rows": 3}``. ``run_gate`` refuses any other result and writes no receipt. A result with
``passed: False`` is still written, as the record of a failed check, and ``promote`` refuses it.

Gate names may use only letters, digits, ``_`` and ``-``, because they become file names and git tag names.

Every receipt carries ``receipt_schema``, the version of the rules it was written under (now 1). A receipt
without it was written by gdkit before versions existed (schema 0). Those can be verified with
``gdkit.audit.verify_run`` but not promoted; see ``gdkit.promote``.

``run_gate(..., capture_log=True)`` captures everything the check writes to stdout and stderr, at the file
descriptor level (subprocesses and earlier logging handlers included, see ``_capture_output``), into
``logs/<gate>.log.partial``, which can be watched while the gate runs. When the check returns or raises, the file
is renamed to ``logs/<gate>.log`` (copied instead if a reader blocks the rename, see ``_finalise_log``) and its
path, sha256, size, how it was finalised, platform and locale encoding go in the receipt. gdkit writes the log, not
the check, so promote's re-run of a cheap gate never writes it.

Optional ``contract_version`` (what a pass means for the project's harness) is stored as given. A census (see
``gdkit.census``), either in the check's result or passed as ``census``, must follow the census rules or no receipt
is written.

When the run has a recorded host, ``run_gate`` refuses to run the check on a machine that differs from it (see
``gdkit.host.require_same_host``); ``check_host=False`` turns this off. The receipt's ``host_check`` says whether the
check ran and against which host record. ``promote``'s re-run of a cheap gate does not check the host, because
it usually runs on another machine.

Receipts are unsigned. Their ``receipt_hash`` is a sha256 of the body, so an accidental edit is
caught. It does not stop someone who recomputes the hash. The promote step is what refuses forgery.
"""

from __future__ import annotations

import contextlib
import hashlib
import inspect
import json
import locale
import os
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from gdkit.census import Census, CensusError, check_result_census, validate_census_dict
from gdkit.stream_hash import canonical_json_bytes

RECEIPTS_DIR = "receipts"
LOGS_DIR = "logs"
PARTIAL_SUFFIX = ".partial"
RECEIPT_SCHEMA = 1
_NAME = re.compile(r"^[A-Za-z0-9_-]+$")


class GateError(ValueError):
    """A gate is misdefined: an unsafe name, or a check that returned no boolean ``passed`` field."""


def check_result(name: str, result: Any) -> None:
    """Raise GateError unless ``result`` is a dict whose ``passed`` field is a bool."""
    if not isinstance(result, dict) or not isinstance(result.get("passed"), bool):
        raise GateError(f"{name}: a gate check must return a dict with a boolean 'passed' field, got {result!r}")


@dataclass(frozen=True)
class Gate:
    """A named check. ``cheap`` gates are re-run at promote time; expensive gates need an attestation."""

    name: str
    cheap: bool
    check: Callable[[Path], Any]

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not _NAME.fullmatch(self.name):
            raise GateError(f"gate name {self.name!r} may use only letters, digits, '_' and '-'")

    @property
    def code_hash(self) -> str:
        """sha256 of the check's source text. A change to the gate changes this hash."""
        return hashlib.sha256(inspect.getsource(self.check).encode("utf-8")).hexdigest()


def receipt_hash(body: dict[str, Any]) -> str:
    """sha256 of a receipt body, excluding any ``receipt_hash`` field."""
    return hashlib.sha256(canonical_json_bytes({k: v for k, v in body.items() if k != "receipt_hash"})).hexdigest()


def sha256_path(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _flush(*streams: Any) -> None:
    for stream in streams:
        try:
            stream.flush()
        except Exception:  # a closed or odd stream must not lose the capture
            pass


@contextlib.contextmanager
def _capture_output(sink: Any) -> Any:
    """Send everything written to stdout and stderr into ``sink`` (the open ``.partial`` log, binary) for the duration.

    Two layers, because each misses what the other catches:
    * file descriptors 1 and 2 are pointed at the sink with ``os.dup2``. That catches subprocesses, C extensions,
      and Python stream objects created earlier that still write to those descriptors, such as a
      ``logging.StreamHandler`` bound to the real ``sys.stderr``;
    * ``sys.stdout`` and ``sys.stderr`` are replaced by a text stream on the sink. That catches ``print`` when the
      current ``sys.stdout`` is not descriptor 1, as under pytest's capture.
    All of them share one file offset, so lines land in the order they were written. Stream objects that were
    created earlier and write somewhere other than descriptors 1 and 2 are not captured.
    """
    old_out, old_err = sys.stdout, sys.stderr
    _flush(old_out, old_err)
    try:
        saved = (os.dup(1), os.dup(2))
    except OSError as exc:
        raise GateError(f"capture_log needs file descriptors 1 and 2, which this process does not have: {exc}") from exc
    text = None
    try:
        os.dup2(sink.fileno(), 1)
        os.dup2(sink.fileno(), 2)
        text = open(os.dup(sink.fileno()), "w", encoding="utf-8", errors="replace", newline="\n", buffering=1)
        sys.stdout = sys.stderr = text
        yield
    finally:
        _flush(text, old_out, old_err, sys.__stdout__, sys.__stderr__)
        sys.stdout, sys.stderr = old_out, old_err
        if text is not None:
            text.close()
        os.dup2(saved[0], 1)
        os.dup2(saved[1], 2)
        os.close(saved[0])
        os.close(saved[1])


def _finalise_log(partial: Path, final: Path) -> tuple[str, bool]:
    """Move the partial log to its final name. Returns (how, whether the partial file was left behind).

    A rename, normally. On Windows a rename fails while another process holds the partial file open without
    delete sharing (a plain Python ``open`` does; PowerShell ``Get-Content -Wait`` and Git Bash ``tail -f`` do not).
    Then the bytes are copied to the final name instead, which readers allow, and the partial file is removed if
    possible. Either way the final log exists, and the receipt hashes it.
    """
    try:
        os.replace(partial, final)
        return "renamed", False
    except PermissionError:
        shutil.copyfile(partial, final)
        try:
            partial.unlink()
            return "copied", False
        except OSError:
            return "copied", True


def _host_check(run_dir: Path, name: str, enabled: bool) -> dict[str, Any]:
    """The same-machine check (see ``gdkit.host.require_same_host``), as recorded in the receipt."""
    if not enabled:
        return {"checked": False, "reason": "disabled by the caller (check_host=False)"}
    from gdkit.host import HostError, require_same_host

    try:
        return require_same_host(run_dir)
    except HostError as exc:
        raise GateError(f"{name}: {exc}") from exc


def run_gate(
    run_dir: str | Path,
    gate: Gate,
    *,
    inputs: list[str],
    run_id: str,
    ledger_head: str,
    capture_log: bool = False,
    contract_version: str | None = None,
    census: Any = None,
    check_host: bool = True,
) -> Path:
    """Run ``gate`` on ``run_dir`` and write its receipt. Returns the receipt path.

    ``inputs`` are paths relative to ``run_dir``, the files the gate reads. Their hashes are taken
    before the check runs, so the receipt records what the check actually saw.
    """
    run_dir = Path(run_dir)
    host_check = _host_check(run_dir, gate.name, check_host)
    input_hashes = {rel: sha256_path(run_dir / rel) for rel in inputs}
    log_bytes = None
    if capture_log:
        logs = run_dir / LOGS_DIR
        logs.mkdir(exist_ok=True)
        partial = logs / f"{gate.name}.log{PARTIAL_SUFFIX}"
        final = logs / f"{gate.name}.log"
        sink = open(partial, "wb")
        try:
            with _capture_output(sink):
                result = gate.check(run_dir)
        finally:
            # Renamed even when the check raises, so the failure's output is kept. Every descriptor on the file
            # is closed first; on Windows an open handle would block the rename.
            sink.close()
            finalised, partial_left = _finalise_log(partial, final)
            log_bytes = final.read_bytes()
    else:
        result = gate.check(run_dir)
    check_result(gate.name, result)
    try:
        check_result_census(result)
        if isinstance(census, Census):
            census.validate()
            census = census.to_dict()
        elif census is not None:
            validate_census_dict(census)
    except CensusError as exc:
        raise GateError(f"{gate.name}: {exc}") from exc
    body: dict[str, Any] = {
        "receipt_schema": RECEIPT_SCHEMA,
        "run_id": run_id,
        "gate": gate.name,
        "cheap": gate.cheap,
        "gate_code_hash": gate.code_hash,
        "inputs": input_hashes,
        "result": result,
        "ledger_head": ledger_head,
        "host_check": host_check,
    }
    if log_bytes is not None:
        body["log"] = {
            "path": f"{LOGS_DIR}/{gate.name}.log",
            "sha256": hashlib.sha256(log_bytes).hexdigest(),
            "bytes": len(log_bytes),
            "finalised": finalised,  # "renamed", or "copied" when a reader blocked the rename
            "partial_left": partial_left,
            # The log keeps the bytes as written, so these say how to read them: other writers (handlers made
            # earlier, child processes) use the locale encoding and the OS line endings; gdkit's own stream
            # writes UTF-8 with LF.
            "platform": sys.platform,
            "locale_encoding": locale.getpreferredencoding(False),
            "gdkit_stream_encoding": "utf-8",
        }
    if contract_version is not None:
        body["contract_version"] = contract_version
    if census is not None:
        body["census"] = census
    body["receipt_hash"] = receipt_hash(body)
    out_dir = run_dir / RECEIPTS_DIR
    out_dir.mkdir(exist_ok=True)
    path = out_dir / f"{gate.name}.json"
    path.write_text(json.dumps(body, sort_keys=True, indent=2), encoding="utf-8")
    return path
