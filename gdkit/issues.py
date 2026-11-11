"""The issue record: problems found while running a study, kept as provenance and ready to send upstream.

    python -m gdkit.issues observe SLUG --kind K --severity S --summary TEXT [--package P --version V
                                        --repo-url URL --run RUN_DIR --gate G --affects-run ID --affects-gate G
                                        --expected TEXT --actual TEXT --reproducer PATH --evidence PATH --error TEXT]
    python -m gdkit.issues again SLUG [--run RUN_DIR] [--note TEXT]
    python -m gdkit.issues triage SLUG [--severity S] [--kind K] [--affects-run ID] [--affects-gate G] [--note TEXT]
    python -m gdkit.issues workaround SLUG --description TEXT [--commit SHA]
    python -m gdkit.issues filed SLUG --url URL
    python -m gdkit.issues resolve SLUG --resolution R [--resolved-version V] [--duplicate-of SLUG] [--note TEXT]
    python -m gdkit.issues amend SLUG [--summary TEXT] [--expected TEXT] [--actual TEXT] [--reproducer PATH]
    python -m gdkit.issues draft SLUG [--project-root .] [--runs-dir runs] [--deny REGEX]
    python -m gdkit.issues list | show SLUG

All commands take ``--log`` (default ``.gdkit/issues/log.jsonl``) and print one JSON object.

Storage. One append-only, hash-chained event log per project, separate from the run ledgers: issues arrive at any
time and span runs, and an entry appended to a run's ledger after its gates would make their receipts stale. Each
entry is ``{seq, ts, event, slug, data, prev_hash, entry_hash}`` and is checked with ``gdkit.ledger.verify``. An
issue's state is the fold of its events, so a status change is a new event, never an edit.

Lifecycle. ``observed`` -> ``triaged`` -> ``drafted`` -> ``filed`` -> ``resolved``. ``workaround`` and ``amended``
events can come at any point. Triage is tracked as its own flag, so drafting or filing an issue does not count as
triaging it.
``observed_again`` adds an occurrence and **reopens** a resolved issue, so a regression blocks again. Resolutions:
``fixed-upstream``, ``adopted`` (we moved to a fixed version), ``wont-fix``, ``not-a-bug``, ``duplicate``.

Links. ``observe --run RUN_DIR`` stores ``observed_in = {run_id, ledger_seq, entry_hash}``, the run ledger's head at
that moment, and adds the run to ``affected_runs``. The run ledger itself is not written. Evidence files are stored
by path (relative to the project root) and sha256.

Dedup. ``--error`` text is normalised (numbers, hex addresses and paths removed) and hashed with the package name
into a ``fingerprint``. Observing a known fingerprint appends ``observed_again`` to the existing issue instead of
creating a new one.

Blocking. ``blocking_issues`` (used by ``promote`` and ``attestable_hash``) returns the unresolved issues that
affect a run, either by run ID or by one of the gates being checked, or that name neither and so affect every run,
and that are ``blocking`` with no workaround or ``degrading`` and still untriaged. Each refusal names the slug and
the commands that clear it. This is a record, not enforcement: anyone who can run these commands can append a
workaround or a downgrade, and nothing checks who did. The issue log's head goes on the promote report and the
model card, so a reviewer can see which state was used.

Upstream. Nothing here contacts any service. Filing upstream is a human step; ``filed`` only records the URL the
person supplies. ``draft`` writes a local upstream draft from allow-listed fields only and refuses it if it
carries private details; see ``draft_issue``.
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import platform
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from gdkit.ledger import GENESIS, LedgerError, _entry_hash, verify

DEFAULT_LOG = ".gdkit/issues/log.jsonl"
KINDS = ("upstream", "data", "harness", "surprise")
SEVERITIES = ("blocking", "degrading", "cosmetic")
RESOLUTIONS = ("fixed-upstream", "adopted", "wont-fix", "not-a-bug", "duplicate")
_SLUG = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")
_URL = re.compile(r"https?://\S+")


AMENDABLE = ("summary", "expected", "actual", "reproducer")


class IssueError(ValueError):
    """An issue event is malformed, refers to an unknown issue, or the log does not verify."""


class DraftError(IssueError):
    """An upstream draft was refused because it would carry private details."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def fingerprint(package: str | None, error: str) -> str:
    """sha256 of the package and the error text with numbers, hex addresses and paths normalised away."""
    text = error.strip().lower()
    text = re.sub(r"""(?:[a-z]:)?[\\/][^\s'"]+""", "<path>", text)
    text = re.sub(r"0x[0-9a-f]+", "0x#", text)
    text = re.sub(r"\d+", "#", text)
    text = re.sub(r"\s+", " ", text)
    return hashlib.sha256(json.dumps([package or "", text]).encode("utf-8")).hexdigest()


class IssueLog:
    """The project's issue event log."""

    def __init__(self, path: str | os.PathLike[str] = DEFAULT_LOG) -> None:
        self.path = Path(path)
        self._entries: list[dict[str, Any]] = []
        if self.path.exists() and self.path.stat().st_size > 0:
            try:
                verify(self.path)
            except LedgerError as exc:
                raise IssueError(f"issue log {self.path} does not verify: {exc}") from exc
            self._entries = [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines() if line.strip()]
        self._size = self.path.stat().st_size if self.path.exists() else 0

    @property
    def head(self) -> str | None:
        return self._entries[-1]["entry_hash"] if self._entries else None

    def entry_hashes(self) -> set[str]:
        """Every entry hash in the log, for checking that a signed head is still part of it."""
        return {entry["entry_hash"] for entry in self._entries}

    def _append(self, event: str, slug: str, data: dict[str, Any]) -> dict[str, Any]:
        size = self.path.stat().st_size if self.path.exists() else 0
        if size != self._size:
            raise IssueError(f"{self.path} changed on disk since it was opened (another writer?); reopen it")
        entry = {
            "seq": len(self._entries),
            "ts": _now(),
            "event": event,
            "slug": slug,
            "data": data,
            "prev_hash": self.head or GENESIS,
        }
        entry["entry_hash"] = _entry_hash(entry)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        self._entries.append(entry)
        self._size = self.path.stat().st_size
        return entry

    # --- state -------------------------------------------------------------------------------

    def issues(self) -> dict[str, dict[str, Any]]:
        """Current state of every issue, folded from its events in order."""
        state: dict[str, dict[str, Any]] = {}
        for entry in self._entries:
            slug, data, event = entry["slug"], entry["data"], entry["event"]
            if event == "observed":
                state[slug] = {
                    **data,
                    "slug": slug,
                    "status": "observed",
                    "triaged": False,
                    "occurrences": 1,
                    "workaround": None,
                    "draft": None,
                    "issue_url": None,
                    "resolution": None,
                    "reopened": 0,
                    "first_seen": entry["ts"],
                }
                continue
            issue = state[slug]
            if event == "observed_again":
                issue["occurrences"] += 1
                issue["affected_runs"] = _union(issue["affected_runs"], data.get("affected_runs", []))
                if issue["status"] == "resolved":
                    # A regression: the old workaround is not assumed to still hold. It is kept for reference and
                    # must be recorded again to clear the block.
                    issue.update(status="observed", resolution=None, reopened=issue["reopened"] + 1,
                                 previous_workaround=issue["workaround"], workaround=None, triaged=False)
            elif event == "triaged":
                for key in ("severity", "kind"):
                    if data.get(key):
                        issue[key] = data[key]
                issue["affected_runs"] = _union(issue["affected_runs"], data.get("affected_runs", []))
                issue["affected_gates"] = _union(issue["affected_gates"], data.get("affected_gates", []))
                issue["triaged"] = True
                if issue["status"] == "observed":
                    issue["status"] = "triaged"
            elif event == "amended":
                for key in AMENDABLE:
                    if data.get(key) is not None:
                        issue[key] = data[key]
            elif event == "drafted":
                issue["draft"] = {"path": data["path"], "sha256": data["sha256"]}
                if issue["status"] in ("observed", "triaged"):
                    issue["status"] = "drafted"
            elif event == "workaround":
                issue["workaround"] = {"description": data["description"], "commit": data.get("commit")}
            elif event == "filed":
                issue["issue_url"] = data["url"]
                if issue["status"] in ("observed", "triaged", "drafted"):
                    issue["status"] = "filed"
            elif event == "resolved":
                issue.update(status="resolved", resolution=data["resolution"],
                             resolved_version=data.get("resolved_version"), duplicate_of=data.get("duplicate_of"))
            issue["last_event"] = entry["ts"]
        return state

    def _known(self, slug: str) -> dict[str, Any]:
        issue = self.issues().get(slug)
        if issue is None:
            raise IssueError(f"{slug}: no such issue in {self.path}")
        return issue

    # --- events ------------------------------------------------------------------------------

    def observe(
        self,
        slug: str,
        *,
        kind: str,
        severity: str,
        summary: str,
        package: str | None = None,
        version: str | None = None,
        repo_url: str | None = None,
        run_dir: str | Path | None = None,
        gate: str | None = None,
        affected_runs: Iterable[str] = (),
        affected_gates: Iterable[str] = (),
        expected: str | None = None,
        actual: str | None = None,
        reproducer: str | None = None,
        evidence: Iterable[str] = (),
        error: str | None = None,
        project_root: str | Path = ".",
    ) -> dict[str, Any]:
        """Record a new issue, or ``observed_again`` on an existing one with the same error fingerprint."""
        if not isinstance(slug, str) or not _SLUG.fullmatch(slug):
            raise IssueError(f"slug {slug!r} must be lowercase kebab-case, like 'transformers-key-names'")
        if kind not in KINDS:
            raise IssueError(f"kind must be one of {KINDS}")
        if severity not in SEVERITIES:
            raise IssueError(f"severity must be one of {SEVERITIES}")
        if not summary or not summary.strip():
            raise IssueError("an issue needs a summary")
        print_ = fingerprint(package, error) if error else None
        link = _link(run_dir, gate)
        runs = _union(list(affected_runs), [link["run_id"]] if link else [])
        if print_ is not None:
            for known in self.issues().values():
                if known.get("fingerprint") == print_:
                    return self._append("observed_again", known["slug"], {"observed_in": link, "affected_runs": runs})
        if slug in self.issues():
            raise IssueError(f"{slug}: already recorded; use 'again' for a new occurrence")
        root = Path(project_root)
        files = []
        for rel in evidence:
            path = root / rel
            if not path.is_file():
                raise IssueError(f"evidence {rel} does not exist under {root}")
            files.append({"path": Path(rel).as_posix(), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
        data = {
            "kind": kind,
            "severity": severity,
            "summary": summary,
            "target": {"package": package, "version": version, "repo_url": repo_url},
            "observed_in": link,
            "affected_runs": runs,
            "affected_gates": _union(list(affected_gates), [gate] if gate else []),
            "expected": expected,
            "actual": actual,
            "reproducer": reproducer,
            "evidence": files,
            "fingerprint": print_,
        }
        return self._append("observed", slug, data)

    def again(self, slug: str, *, run_dir: str | Path | None = None, note: str | None = None) -> dict[str, Any]:
        self._known(slug)
        link = _link(run_dir, None)
        return self._append("observed_again", slug,
                            {"observed_in": link, "affected_runs": [link["run_id"]] if link else [], "note": note})

    def triage(
        self,
        slug: str,
        *,
        severity: str | None = None,
        kind: str | None = None,
        affected_runs: Iterable[str] = (),
        affected_gates: Iterable[str] = (),
        note: str | None = None,
    ) -> dict[str, Any]:
        self._known(slug)
        if severity is not None and severity not in SEVERITIES:
            raise IssueError(f"severity must be one of {SEVERITIES}")
        if kind is not None and kind not in KINDS:
            raise IssueError(f"kind must be one of {KINDS}")
        return self._append("triaged", slug, {"severity": severity, "kind": kind, "affected_runs": list(affected_runs),
                                              "affected_gates": list(affected_gates), "note": note})

    def amend(self, slug: str, **fields: str | None) -> dict[str, Any]:
        """Replace the text of summary, expected, actual or reproducer. The earlier text stays in the log."""
        self._known(slug)
        unknown = set(fields) - set(AMENDABLE)
        if unknown:
            raise IssueError(f"only {', '.join(AMENDABLE)} can be amended, not {', '.join(sorted(unknown))}")
        changes = {key: value for key, value in fields.items() if value is not None}
        if not changes:
            raise IssueError(f"amend needs at least one of: {', '.join(AMENDABLE)}")
        if "summary" in changes and not changes["summary"].strip():
            raise IssueError("the summary may not be empty")
        return self._append("amended", slug, changes)

    def workaround(self, slug: str, *, description: str, commit: str | None = None) -> dict[str, Any]:
        self._known(slug)
        if not description or not description.strip():
            raise IssueError("a workaround needs a description")
        if commit is not None and not re.fullmatch(r"[0-9a-f]{7,40}", commit):
            raise IssueError("commit must be a 7 to 40 character hex git SHA")
        return self._append("workaround", slug, {"description": description, "commit": commit})

    def filed(self, slug: str, *, url: str) -> dict[str, Any]:
        """Record the URL of an upstream issue a person filed. gdkit does not file or check it."""
        self._known(slug)
        if not _URL.fullmatch(url or ""):
            raise IssueError("url must be an http(s) URL")
        return self._append("filed", slug, {"url": url})

    def resolve(
        self,
        slug: str,
        *,
        resolution: str,
        resolved_version: str | None = None,
        duplicate_of: str | None = None,
        note: str | None = None,
    ) -> dict[str, Any]:
        self._known(slug)
        if resolution not in RESOLUTIONS:
            raise IssueError(f"resolution must be one of {RESOLUTIONS}")
        if resolution == "duplicate":
            if duplicate_of is None:
                raise IssueError("a duplicate needs --duplicate-of SLUG")
            self._known(duplicate_of)
        return self._append("resolved", slug, {"resolution": resolution, "resolved_version": resolved_version,
                                               "duplicate_of": duplicate_of, "note": note})


def _union(first: Iterable[str], second: Iterable[str]) -> list[str]:
    return list(dict.fromkeys([*first, *second]))


def _link(run_dir: str | Path | None, gate: str | None) -> dict[str, Any] | None:
    """The run ledger's head at observation time. The run ledger is only read."""
    if run_dir is None:
        return None
    path = Path(run_dir) / "ledger.jsonl"
    try:
        verify(path)
    except (LedgerError, OSError) as exc:
        raise IssueError(f"cannot link to run {run_dir}: its ledger does not verify ({exc})") from exc
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not lines:
        raise IssueError(f"cannot link to run {run_dir}: its ledger is empty")
    head = json.loads(lines[-1])
    return {"run_id": head["run_id"], "ledger_seq": head["seq"], "entry_hash": head["entry_hash"], "gate": gate}


def _affects(issue: dict[str, Any], run_id: str, gates: Iterable[str]) -> bool:
    runs, gate_list = issue.get("affected_runs") or [], issue.get("affected_gates") or []
    if not runs and not gate_list:
        return True  # names neither: project-wide
    return run_id in runs or bool(set(gates) & set(gate_list))


BLOCKED_NO_WORKAROUND = "blocking with no workaround"
BLOCKED_UNTRIAGED = "degrading and not triaged"


def blocking_reason(issue: dict[str, Any]) -> str | None:
    """Why an unresolved issue blocks, or None."""
    if issue["status"] == "resolved":
        return None
    if issue["severity"] == "blocking" and issue["workaround"] is None:
        return BLOCKED_NO_WORKAROUND
    if issue["severity"] == "degrading" and not issue.get("triaged"):
        return BLOCKED_UNTRIAGED
    return None


def clearing_commands(slug: str, reason: str) -> list[str]:
    """The commands that clear ``reason`` for ``slug``, and only those."""
    tool = "python -m gdkit.issues"
    resolve = f"{tool} resolve {slug} --resolution fixed-upstream|adopted|wont-fix|not-a-bug|duplicate"
    if reason == BLOCKED_NO_WORKAROUND:
        return [
            f'{tool} workaround {slug} --description "..."',
            f"{tool} triage {slug} --severity degrading",
            resolve,
        ]
    return [f"{tool} triage {slug}", resolve]


def affecting(log: IssueLog, run_id: str, gates: Iterable[str]) -> list[dict[str, Any]]:
    gates = list(gates)
    return [issue for issue in log.issues().values() if _affects(issue, run_id, gates)]


def blocking_issues(log_path: str | Path, run_id: str, gates: Iterable[str]) -> tuple[str | None, list[str]]:
    """(issue log head or None when there is no log, refusal messages). Raises IssueError if the log is damaged."""
    path = Path(log_path)
    if not path.exists():
        return None, []
    log = IssueLog(path)
    messages = []
    for issue in affecting(log, run_id, gates):
        reason = blocking_reason(issue)
        if reason:
            slug = issue["slug"]
            messages.append(f"issue {slug} is {reason}. Clear it with one of: " + " ; ".join(clearing_commands(slug, reason)))
    return log.head, messages


# --- upstream drafts -------------------------------------------------------------------------

DRAFTS_DIR = ".gdkit/issues/drafts"
DENY_FILE = ".gdkit/issues/deny.txt"
_HEX64 = re.compile(r"(?<![0-9a-fA-F])[0-9a-fA-F]{64}(?![0-9a-fA-F])")
_HOME = re.compile(r"(?i)(?:[a-z]:\\users\\[^\\\s]+|/home/[^/\s]+|/users/[^/\s]+)")
_RUNS_PATH = re.compile(r"(?<![A-Za-z0-9_.-])runs[/\\]")
_GITHUB = re.compile(r"https://github\.com/[^/\s]+/[^/\s]+?/?")
_LANGUAGES = {".py": "python", ".sh": "bash", ".ps1": "powershell", ".json": "json", ".yaml": "yaml", ".yml": "yaml"}


def _word(token: str) -> re.Pattern[str]:
    return re.compile(r"(?i)(?<![A-Za-z0-9])" + re.escape(token) + r"(?![A-Za-z0-9])")


def _run_ledger_host(runs_dir: Path, run_id: str) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """(host summary from the run's ledger, full host.json or {}) for a linked run. Read-only."""
    run = runs_dir / run_id
    summary = None
    ledger = run / "ledger.jsonl"
    if ledger.is_file():
        for line in ledger.read_text(encoding="utf-8").splitlines():
            if line.strip():
                entry = json.loads(line)
                if entry.get("event") and entry.get("op") == "host":
                    summary = entry["params"].get("summary")
    host_file = run / "host.json"
    full = json.loads(host_file.read_text(encoding="utf-8")) if host_file.is_file() else {}
    return summary, full


def _private_rules(issue: dict[str, Any], root: Path, runs_dir: Path, deny: Iterable[str]) -> list[tuple[str, Any]]:
    """(rule name, compiled pattern) pairs the draft must not match."""
    rules: list[tuple[str, Any]] = [
        ("a 64-character hex hash (ledger and file hashes stay private)", _HEX64),
        ("a home path", _HOME),
        ("a runs/ path", _RUNS_PATH),
    ]
    home = str(Path.home())
    if len(home) > 3:
        rules.append(("a home path", re.compile(re.escape(home), re.I)))
        rules.append(("a home path", re.compile(re.escape(Path.home().as_posix()), re.I)))
    run_ids = set(issue.get("affected_runs") or [])
    if issue.get("observed_in"):
        run_ids.add(issue["observed_in"]["run_id"])
    if runs_dir.is_dir():
        run_ids.update(p.name for p in runs_dir.iterdir() if p.is_dir())
    users, hosts = set(), {platform.node()}
    try:
        users.add(getpass.getuser())
    except Exception:
        pass
    if issue.get("observed_in"):
        _, full = _run_ledger_host(runs_dir, issue["observed_in"]["run_id"])
        users.add(full.get("user") or "")
        hosts.add((full.get("platform") or {}).get("node") or "")
    rules += [("a run id", _word(r)) for r in sorted(run_ids) if len(r) >= 2]
    rules += [("this machine's username", _word(u)) for u in sorted(users) if len(u) >= 2]
    rules += [("this machine's hostname", _word(h)) for h in sorted(hosts) if len(h) >= 2]
    patterns = list(deny)
    deny_file = root / DENY_FILE
    if deny_file.is_file():
        patterns += [line.strip() for line in deny_file.read_text(encoding="utf-8").splitlines()
                     if line.strip() and not line.lstrip().startswith("#")]
    rules += [(f"deny pattern {p}", re.compile(p)) for p in patterns]
    return rules


def _render(issue: dict[str, Any], root: Path, runs_dir: Path) -> str:
    """The draft, built only from allow-listed fields."""
    target = issue.get("target") or {}
    summary = None
    if issue.get("observed_in"):
        summary, _ = _run_ledger_host(runs_dir, issue["observed_in"]["run_id"])
    if summary:
        python, system = summary["python"], summary["platform"]
        environment = f"Python {python['version']} ({python['implementation']}), {system['system']} {system['machine']}"
    else:
        environment = (f"Python {platform.python_version()} ({platform.python_implementation()}), "
                       f"{platform.system()} {platform.machine()}")
    lines = [f"# {issue['summary'].strip()}", ""]
    if target.get("package"):
        lines.append(f"**Package:** {target['package']} {target.get('version') or ''}".rstrip())
    lines += [f"**Environment:** {environment}", ""]
    lines += ["## What happened", "", (issue.get("actual") or "(not recorded)").strip(), ""]
    lines += ["## What was expected", "", (issue.get("expected") or "(not recorded)").strip(), ""]
    if issue.get("reproducer"):
        path = root / issue["reproducer"]
        if not path.is_file():
            raise DraftError(f"reproducer {issue['reproducer']} not found under {root}; fix the file or "
                             f"python -m gdkit.issues amend {issue['slug']} --reproducer PATH")
        code = path.read_text(encoding="utf-8").rstrip("\n")
        lines += ["## Reproducer", "", f"```{_LANGUAGES.get(path.suffix, '')}", code, "```", ""]
    return "\n".join(lines)


def draft_issue(
    log: IssueLog,
    slug: str,
    *,
    project_root: str | Path = ".",
    runs_dir: str | Path | None = None,
    deny: Iterable[str] = (),
) -> dict[str, Any]:
    """Write an upstream issue draft for ``slug`` and record it. Contacts nothing.

    The draft is built only from allow-listed fields: summary, package and version, Python and OS (from the linked
    run's host summary, else this machine), what happened, what was expected, and the reproducer file. Evidence,
    fingerprints, run links, workarounds and affected gates are never included. The rendered text is then checked,
    and refused if it contains a 64-character hex hash, a home path, a ``runs/`` path, a known run ID, this
    machine's (or the linked run's) username or hostname, or a pattern from ``.gdkit/issues/deny.txt``. A refused
    draft writes nothing and records nothing; ``amend`` (or editing the reproducer file) is the way forward.
    """
    issue = log._known(slug)
    root = Path(project_root)
    runs = Path(runs_dir) if runs_dir is not None else root / "runs"
    text = _render(issue, root, runs)
    found = []
    for number, line in enumerate(text.splitlines(), start=1):
        for name, pattern in _private_rules(issue, root, runs, deny):
            if pattern.search(line) and (name, number) not in found:
                found.append((name, number))
    if found:
        where = "; ".join(f"{name} on line {number}" for name, number in found)
        raise DraftError(
            f"the draft for {slug} is refused: it contains {where}. Change the text with python -m gdkit.issues "
            f"amend {slug} --summary/--expected/--actual TEXT, edit the reproducer file, or adjust {DENY_FILE}"
        )
    drafts = root / DRAFTS_DIR
    drafts.mkdir(parents=True, exist_ok=True)
    (drafts / ".gitignore").write_bytes(b"*\n")
    data = (text + "\n").encode("utf-8")
    rel = f"{DRAFTS_DIR}/{slug}.md"
    (root / rel).write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    log._append("drafted", slug, {"path": rel, "sha256": digest})
    repo = (issue.get("target") or {}).get("repo_url") or ""
    url = repo.rstrip("/") + "/issues/new" if _GITHUB.fullmatch(repo) else None
    return {"path": rel, "sha256": digest, "new_issue_url": url}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="gdkit issues")
    parser.add_argument("--log", default=DEFAULT_LOG)
    commands = parser.add_subparsers(dest="command", required=True)
    observe = commands.add_parser("observe")
    observe.add_argument("slug")
    observe.add_argument("--kind", required=True, choices=KINDS)
    observe.add_argument("--severity", required=True, choices=SEVERITIES)
    observe.add_argument("--summary", required=True)
    for name in ("package", "version", "repo-url", "run", "gate", "expected", "actual", "reproducer", "error"):
        observe.add_argument(f"--{name}")
    observe.add_argument("--affects-run", action="append", default=[])
    observe.add_argument("--affects-gate", action="append", default=[])
    observe.add_argument("--evidence", action="append", default=[])
    observe.add_argument("--project-root", default=".")
    again = commands.add_parser("again")
    again.add_argument("slug")
    again.add_argument("--run")
    again.add_argument("--note")
    triage = commands.add_parser("triage")
    triage.add_argument("slug")
    triage.add_argument("--severity", choices=SEVERITIES)
    triage.add_argument("--kind", choices=KINDS)
    triage.add_argument("--affects-run", action="append", default=[])
    triage.add_argument("--affects-gate", action="append", default=[])
    triage.add_argument("--note")
    workaround = commands.add_parser("workaround")
    workaround.add_argument("slug")
    workaround.add_argument("--description", required=True)
    workaround.add_argument("--commit")
    filed = commands.add_parser("filed")
    filed.add_argument("slug")
    filed.add_argument("--url", required=True)
    resolve = commands.add_parser("resolve")
    resolve.add_argument("slug")
    resolve.add_argument("--resolution", required=True, choices=RESOLUTIONS)
    resolve.add_argument("--resolved-version")
    resolve.add_argument("--duplicate-of")
    resolve.add_argument("--note")
    amend = commands.add_parser("amend")
    amend.add_argument("slug")
    for name in AMENDABLE:
        amend.add_argument(f"--{name}")
    draft = commands.add_parser("draft")
    draft.add_argument("slug")
    draft.add_argument("--project-root", default=".")
    draft.add_argument("--runs-dir")
    draft.add_argument("--deny", action="append", default=[], help="an extra regular expression the draft must not match")
    commands.add_parser("list")
    show = commands.add_parser("show")
    show.add_argument("slug")
    args = parser.parse_args(argv)

    try:
        log = IssueLog(args.log)
        if args.command == "list":
            out: dict[str, Any] = {"ok": True, "head": log.head, "issues": [
                {k: issue[k] for k in ("slug", "kind", "severity", "status", "occurrences")}
                | {"workaround": issue["workaround"] is not None, "blocks": blocking_reason(issue)}
                for issue in log.issues().values()
            ]}
        elif args.command == "show":
            out = {"ok": True, "issue": log._known(args.slug)}
        elif args.command == "draft":
            result = draft_issue(log, args.slug, project_root=args.project_root, runs_dir=args.runs_dir, deny=args.deny)
            out = {"ok": True, **result, "note": "gdkit contacted nothing. Review the draft, file it by hand, then "
                   f"record it: python -m gdkit.issues filed {args.slug} --url URL"}
        else:
            if args.command == "observe":
                entry = log.observe(
                    args.slug, kind=args.kind, severity=args.severity, summary=args.summary, package=args.package,
                    version=args.version, repo_url=args.repo_url, run_dir=args.run, gate=args.gate,
                    affected_runs=args.affects_run, affected_gates=args.affects_gate, expected=args.expected,
                    actual=args.actual, reproducer=args.reproducer, evidence=args.evidence, error=args.error,
                    project_root=args.project_root,
                )
            elif args.command == "again":
                entry = log.again(args.slug, run_dir=args.run, note=args.note)
            elif args.command == "triage":
                entry = log.triage(args.slug, severity=args.severity, kind=args.kind, affected_runs=args.affects_run,
                                   affected_gates=args.affects_gate, note=args.note)
            elif args.command == "amend":
                entry = log.amend(args.slug, **{name: getattr(args, name) for name in AMENDABLE})
            elif args.command == "workaround":
                entry = log.workaround(args.slug, description=args.description, commit=args.commit)
            elif args.command == "filed":
                entry = log.filed(args.slug, url=args.url)
            else:
                entry = log.resolve(args.slug, resolution=args.resolution, resolved_version=args.resolved_version,
                                    duplicate_of=args.duplicate_of, note=args.note)
            out = {"ok": True, "event": entry["event"], "slug": entry["slug"], "seq": entry["seq"],
                   "status": log.issues()[entry["slug"]]["status"]}
    except Exception as exc:  # one JSON answer, not a traceback
        out = {"ok": False, "message": f"{type(exc).__name__}: {exc}"}
    print(json.dumps(out, ensure_ascii=False))
    return 0 if out["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
