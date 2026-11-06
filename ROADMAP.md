# gdkit roadmap

Status as of 2026-11-06. The design doc is kept outside this repository. Item numbers refer to its Must, Should and
Later lists.

## Done (local commits, not pushed)

- Write-through ledger, hash chain and stream hasher (Must 1, partly)
- `promote` with receipt binding, cheap-gate re-run and SSH attestation (Must 7, 11b, 11c, 11e, partly)
- Papers With Code lookups recorded in the ledger, with UNAVAILABLE entries (Must 11a, 11d). `promote` and attestation
  refuse while a lookup's latest entry for the same request is UNAVAILABLE, so a successful retry clears the block.
- `gdkit.clinic`: the /overfit-clinic interview engine. Asks one question at a time, checks each answer, saves after every
  answer, resumes, and never fills in a default (Must 4, engine only)
- `/overfit-clinic` skill in `skills/overfit-clinic/`, driving `gdkit.skill_api` (`next`, `set --value-file`)
- Pre-registration freeze (Must 11f): the answers are the run's first ledger entry, and `promote` and attestation refuse
  changed answers, naming them. Detection only.
- `gdkit.doctor`: validates the project answers against the interview questions, per mode (Must 3)
- Model and dataset card generators with audit against the ledger (Must 2)
- Attestation CLI (`gdkit.cli attest`) and the GitHub Actions workflow (not yet run on GitHub)

102 tests pass locally with `python -m pytest`.

## Next

1. **Attestation custody.** Chosen: CI with a required-reviewer environment and an SSH signing key stored as a secret.
   This needs the GitHub repository, the environment and the key. Not yet set up or run.
2. **Run `/overfit-clinic` on a real study** in explore or registered mode, and check the result with `gdkit.doctor`.
3. **Contamination check and environment capture merge** (Must 5, 6).
4. **A defined general-capability check, and the eval harness and prompt hashes in the setup record** (Must 8, 9).
5. **`gdkit-upgrade` and `installed.json`** (Must 10).

## Open decisions and unknowns

- **Same-user bypass check (deferred).** A review of another project's approval gates found three patterns that the same
  user can get around: an owner flag read from an environment variable, approvals written as text rows that nothing checks
  for authorship, and a local pre-commit hook that `--no-verify` skips, with no server-side check. gdkit must not repeat
  these. Each approval needs a key the agent cannot reach, and each hook needs a server-side check before it counts as
  enforcement. Not started.
- The attestation workflow has not run on GitHub.
- The `pwc` install command is not yet confirmed.
- The `gdkit` name is free on PyPI. Trademarks and GitHub organisation names are not checked.
- Four gaps raised in review are not yet in the design: checkpoint laundering, explore-mode p-hacking, out-of-band data
  changes, and non-deterministic reruns. Decide whether to add them.

## Later

- **v1.1 (Should):** seed and determinism audit, checkpoint lineage, hardware and precision flags, cost budget, Croissant
  export, paper search skill, update check and `just-upgraded` marker, and guard tests across runs.
- **v2 (Later):** experiment tracker sync, licence and PII scan, automated incident matcher, external anchor for
  attestations.

## Known limits

See the list in `README.md`.
