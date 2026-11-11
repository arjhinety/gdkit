# Changelog

All notable changes are listed here. The format follows Keep a Changelog, and the project uses semantic versions.

## Unreleased

### Provenance slice 4

- `python -m gdkit.issues draft SLUG` writes an upstream issue draft to `.gdkit/issues/drafts/<slug>.md` (the folder
  is git-ignored) from allow-listed fields only, and records a `drafted` event with its sha256. The rendered text is
  refused, with nothing written or recorded, if it contains a 64-character hex hash, a home path, a `runs/` path, a
  known run ID, this machine's or the linked run's username or hostname, or a pattern from `.gdkit/issues/deny.txt`.
  gdkit prints the GitHub new-issue URL and contacts nothing.
- `amend` replaces an issue's summary, expected, actual or reproducer text, so a refused draft can be fixed.
- Fixed: filing an untriaged degrading issue cleared its promote block. Triage is now its own flag, reset when an
  issue reopens.

### Provenance slices 1 to 3

- Receipts carry `receipt_schema` (1). `promote` applies each schema's rules from `RECEIPT_RULES`. A receipt without
  the field is schema 0 (written by 8cf73ec or 9027e56): verify-only, refused by `promote` and `attestable_hash` with
  "re-run the gate". An unknown or non-integer schema is refused. **Breaking:** receipts written by 9027e56 must be
  re-run to promote.
- `gdkit.audit.verify_run`: a read-only report on every receipt under its own schema (hash, run, which ledger entry it
  was written against, inputs and log as match/changed/absent, code, pass, attestation including legacy
  `attest/<run_id>` tags via `git_attestation(..., schema=0)`).
- `run_gate(capture_log=True)` captures stdout and stderr at the file-descriptor level (subprocesses and earlier
  logging handlers included) into `logs/<gate>.log.partial` (watchable while the gate runs), renames it to
  `logs/<gate>.log` when the check returns or raises, and hashes it into the receipt. If a Windows reader blocks the
  rename, the log is copied instead (`finalised: copied`, `partial_left`). The receipt records the platform and locale
  encoding next to the hash, because the log keeps the bytes as written. A changed log
  refuses promote; an absent one is noted in the report. Optional `contract_version` and `census` receipt fields.
- `Ledger.record_event` marks entries that are not data steps; cards leave them out of the dataset counts. Cards carry
  `card_schema` (2), and `audit_card` reports an older card as "regenerate it".
- `gdkit.run` and `gdkit.host`: start a run (freeze first, so a refused freeze can be retried), record host, scope,
  config, decisions and files as exact bytes with hashes. The model card reads `config.json` (refusing it if changed
  since recorded), falls back to `model_setup.json`, and shows the recorded hashes, host summary and decisions.
- `gdkit.issues`: a project-wide hash-chained issue log with observe, again (reopens a resolved issue), triage,
  workaround, filed and resolve; error fingerprints for dedup; links to a run's ledger head without writing it.
  `promote` and `attestable_hash` refuse on unresolved issues affecting the run that are blocking with no workaround or
  degrading and untriaged, and report the issue log head. The model card lists affecting issues.

### Changed (breaking for gate authors)

- A gate's check must return a dict with a boolean `passed` field. `run_gate` refuses any other result. `promote` and
  `gdkit.cli attest` refuse a receipt whose result is not `passed: true`. Before this, a gate that reported a failure
  was promoted as long as its re-run gave the same failure.
- Attestation tags are now one per expensive gate, `attest/<run_id>/<gate>`. The verifier receives the gate name, and
  the tag message must be exactly the receipt hash (it was a substring check). Before this, a run could attest only one
  expensive gate.
- Gate names may use only letters, digits, `_` and `-`.
- The attestation workflow is split into two jobs. Repository code (the gate registry) runs only in a job with no
  secret, and the job holding the signing key runs no repository code.
- Paper lookup raw responses are named by the ledger position of their entry (`000003.json`), not by counting files.

### Added

- `replaced_unset`: when a question left `UNSET` or skipped in explore mode is answered later,
  `skill_api set` and the clinic list it under `replaced_unset` in `.gdkit/project.yaml`. `freeze_run` copies the list
  into the preregistration entry (an empty list when there is none), `promote` refuses if the list changes after the
  freeze, and the model card shows it as `preregistration_replaced_unset`. This records, and does not block, answers that
  may have been chosen after an explore run saw the data.
- The ledger detects a second writer (the file changed since this object's last write) and refuses to append.
- Explore-to-registered cross-check. A blank reply in explore mode is recorded under `skipped_in_explore`, and a later
  answer to it joins `replaced_unset`. A new `skill_api change` command is the only way to change a real answer, and
  it declares the change under `changed_after_explore`. A registered `freeze_run` compares the answers with every
  explore-mode freeze in the sibling runs (`runs_dir`, by default the directory that holds the run's own directory,
  which is `runs/` in the `runs/<run_id>/` layout) and refuses any difference that is not
  declared, or any earlier explore-freeze ledger that does not verify. The refusal prints a `skill_api declare`
  command for each difference. `declare` adds a question to `replaced_unset` or `changed_after_explore` without
  changing its answer, which covers questions never asked during the explore run and answers dropped since. The entry
  records `changed_after_explore`, `explore_runs_checked` and `explore_runs_dir` (the folder searched, relative to the
  project root that holds `.gdkit/`), and the model card shows all three. Declarations are records, not approvals:
  nothing checks who ran `declare`.
- The model card has four new fields (`preregistration_replaced_unset`, `preregistration_changed_after_explore`,
  `preregistration_explore_runs_checked`, `preregistration_explore_runs_dir`), so a card written before this change fails `audit_card` until it is
  regenerated.

### Changed (breaking for explore-mode users)

- `skill_api set` with a blank value in explore mode now succeeds and records the skip. It was refused before. The
  clinic and `next` no longer ask a skipped question again in explore mode. They do ask it in registered mode.
- A registered `freeze_run` can now refuse because of an earlier explore run in the same runs directory.

### Fixed

- The clinic and `skill_api` asked nothing in registered mode for required answers left `UNSET` in explore mode, and
  `set` refused them, so a study could not move from explore to registered mode.
- The dataset card counted the preregistration entry as the raw data, so every registered run reported `raw_rows: 0`.
- The clinic accepted `nan` and `inf` as numbers, which later crashed the freeze. It also saved whole numbers as
  decimals (`42` as `42.0`). The validator now refuses non-finite numbers too.
- A half-written or non-object ledger line raised a raw `JSONDecodeError` or `AttributeError`. It is now a `LedgerError`
  naming the line, so `promote` refuses cleanly. The line is reported, not repaired.
- A `pwc` response that was valid JSON but not the expected shape crashed the lookup after saving the raw file and
  before writing a ledger entry. It is now recorded as UNAVAILABLE, with no raw file.

### Earlier

- First public release candidate. Provenance ledger with a hash chain, stream hashing, gate receipts, `promote()` with
  receipt binding and re-run of cheap gates, SSH attestation, pre-registration freeze, Papers With Code lookups recorded
  in the ledger, model and dataset cards, the `/overfit-clinic` interview and its validator, and the attestation workflow
  (not yet run on GitHub).
