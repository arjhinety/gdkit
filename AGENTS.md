# AGENTS.md

Instructions for any coding agent working in this repository. `CONTRIBUTING.md` has the human version; this
file adds what an agent tends to get wrong.

## What gdkit is

A small, strict toolkit for pre-registered studies: a hash-chained JSONL ledger (`ledger.py`), the
pre-registration interview and freeze (`interview.py`, `doctor.py`, `skill_api.py`, `prereg.py`), gate receipts
and log capture (`gates.py`), promotion and SSH attestation (`promote.py`, `cli.py`), run and host recording
(`run.py`, `host.py`), the issue log and upstream drafts (`issues.py`), census rules (`census.py`), the
append-only check (`append_only.py`), recorded pwc lookups (`pwc_lookup.py`) and run audit (`audit.py`).

gdkit refuses things on purpose. A change that makes it accept more needs a stated reason, a test, and a line in
the README's *Known limits* if it opens a gap.

## Setup and checks

```sh
python -m pip install -e ".[test]"
python -m pytest            # SSH attestation tests skip without git and ssh-keygen
```

CI (`.github/workflows/test.yml`) runs the suite on Linux only; run it locally on Windows when you touch log
capture, file writing or paths. `attest.yml` and `append-only.yml` are the
workflows downstream studies copy.

## Rules for a change

- Keep the suite green; add a test for each new refusal and each bug fix. Where a number is computed, assert the
  literal expected value from a worked example, not just "it ran".
- Prose and code move together: if a README, ROADMAP or skill rule changes, change the code or test that
  enforces it in the same commit. Numeric claims in prose must use the same units and formulas as the code.
- Add a line under "Unreleased" in `CHANGELOG.md`.
- Do not edit `vendor/` by hand; it is pinned by hash in `SOURCE.lock`.
- File formats are versioned. Receipts carry `receipt_schema` (0 is verify-only, 1 is current) and
  `promote.RECEIPT_RULES` dispatches on it. Do not change what an existing schema means; add a new one.
- Commit messages start with `feat`, `fix`, `docs`, `test`, `refactor`, `ci` or `chore`. Tags named `attest/...`
  are reserved for the attestation workflow.

## Bytes, not text

Anything gdkit hashes is hashed as raw bytes: captured gate logs, pwc responses (`pwc_lookup` writes them to
`paper_lookups/NNNNNN.json`), and whatever a study hashes on its side. On Windows those bytes often contain CRLF.
A study whose `.gitattributes` says `* text=auto eol=lf` will then commit LF and every hash fails on a fresh
clone, while the ledger itself still verifies. WeightSpaceAbliteration hit exactly this on 2026-11-11. When you
write or test anything hashed:

- read and write bytes (`read_bytes` / `write_bytes`, `git cat-file blob`), never text mode;
- in tests, use fixtures with CRLF and non-ASCII bytes, and a temporary git repo with `core.autocrlf=false`;
- tell downstream users to mark hashed run files `-text`.

`pwc-cli` crashes on Windows when it prints non-cp1252 JSON into a pipe; `pwc_lookup` then records an
`UNAVAILABLE` entry, which blocks promotion. Run it with `PYTHONUTF8=1`.

## Downstream

WeightSpaceAbliteration pins gdkit by commit in its `pyproject.toml` (currently `fa1e4d5`). A change to a
receipt, ledger or log format here can make its existing runs refuse, and the same-machine check makes any gdkit
upgrade in the middle of a run refuse later gates. Say in the commit message whether a change affects existing
runs.
