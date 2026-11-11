# Changelog

All notable changes are listed here. The format follows Keep a Changelog, and the project uses semantic versions.

## Unreleased

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
