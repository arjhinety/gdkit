# gdkit roadmap

Status as of 2026-11-11. The design doc is kept outside this repository. Item numbers refer to its Must, Should and
Later lists.

## Done

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

- Review fixes (2026-11-11): gates must return `passed`, one attestation tag per expensive gate, dataset card skips the
  preregistration entry, explore-to-registered `UNSET` answers re-asked and recorded as `replaced_unset`, finite numbers
  only, clean errors for cut-off ledger lines and malformed `pwc` responses, second-writer detection, the attestation
  workflow split so the signing job runs no repository code, and the explore-to-registered cross-check at the freeze.
  See `CHANGELOG.md`.

- Provenance slices 1 to 3 (designed with a Fable design review, 2026-11-11): receipt schema versions with
  verify-only schema 0 (`gdkit.audit.verify_run`), file-descriptor-level gate log capture, event entries left out of
  dataset counts, card schema; run recording (`gdkit.run`, `gdkit.host`, `config.json` replacing `model_setup.json`);
  the project issue log (`gdkit.issues`) with the promote and attest block. Checked read-only against
  WeightSpaceAbliteration's two runs: all four receipts verify as schema 0, stale, not promotable.

- Provenance slice 4 (2026-11-11): upstream issue drafts (`gdkit.issues draft`, `amend`) from allow-listed fields,
  refused when they carry private details; triage tracked as its own flag.

- Provenance slice 5 (2026-11-11): census rules (`gdkit.census`), the same-machine check in `run_gate`, the
  append-only check (`gdkit.append_only` and its workflow), and one attestation tag per receipt that signs the issue
  log head. The workflows are YAML-checked only until they run on GitHub.

323 tests pass locally with `python -m pytest` (1 skipped on Windows).

## Next

1. **Attestation custody.** Chosen: CI with a required-reviewer environment and an SSH signing key stored as a secret.
   This needs the GitHub repository, the environment and the key. Not yet set up or run.
2. **Run `/overfit-clinic` on a real study** in explore or registered mode, and check the result with `gdkit.doctor`.
3. **Contamination check and environment capture merge** (Must 5, 6).
4. **A defined general-capability check, and the eval harness and prompt hashes in the setup record** (Must 8, 9).
5. **`gdkit-upgrade` and `installed.json`** (Must 10).
6. **Upgrade WeightSpaceAbliteration** to current gdkit: its vendored copy is 8cf73ec plus an uncommitted
   `contract_version`/`census` patch while its CI installs clean 8cf73ec. Its gates need a `passed` field and a
   re-run to promote.

## Open decisions and unknowns

- **Gate log bytes differ by OS (decided 2026-11-11: keep raw).** On Windows one log mixes UTF-8 + LF (`print`) with
  the locale encoding + CRLF (child processes, handlers made earlier). The log keeps the exact bytes; the receipt
  records the platform and locale encoding so a reader can decode it. Cross-OS hashes of the same check differ.
  Not tested: a handler bound to a real interactive Windows console, which writes with `WriteConsoleW`.
- **Late notes (decided 2026-11-11: keep the rule).** Everything is recorded before the gates; late facts go in the
  issue log.

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
- **Explore-mode p-hacking.** The risk: a study looks at the data in explore mode, then writes its registered answers
  (threshold, metric, kill criteria) to fit what it saw, and the answers look as if they were decided up front.
  - The 2026-11-11 fix that lets required `UNSET` answers be filled in registered mode makes this path easier. Before it,
    the clinic could not complete such a study at all. To offset that, every such answer is now recorded under
    `replaced_unset` in the project file, the preregistration entry and the model card.
  - Done (2026-11-11, decided by the owner): explore-mode skips are recorded under `skipped_in_explore`, and a later
    answer to one joins `replaced_unset`. A real answer can only be changed with `skill_api change`, which declares it
    under `changed_after_explore`. A registered `freeze_run` compares the answers with every explore-mode freeze among
    the sibling runs (by default the other `<dir>/ledger.jsonl` files next to the run's own directory, that is
    `runs/*/ledger.jsonl` in the `runs/<run_id>/` layout) and **refuses** any difference that is not declared, including
    changed real answers, and refuses if one of those ledgers does not verify. Declared differences are frozen with the
    run, and the run IDs checked are recorded as `explore_runs_checked`. All three show on the model card. The refusal
    prints a `skill_api declare` command per difference, which covers questions never asked during the explore run
    and answers dropped since.
  - The folder the freeze searched is saved as `explore_runs_dir`, relative to the project root (the folder holding
    `.gdkit/`), and shown on the model card as `preregistration_explore_runs_dir`. An empty `explore_runs_checked` next
    to an unexpected folder tells a reviewer that the search had nothing to compare, for example because the run's
    ledger was not in the `runs/<run_id>/` layout.
  - Remaining gaps:
    - **Visible, not prevented.** The refusal is cleared by a `skill_api declare` command that anyone, an agent
      included, can run, and nothing checks who ran it. That is the pattern the same-user bypass item above names:
      an approval written as a plain entry that nothing checks for authorship. The real protection is the record on
      the model card. A true block would need a signed approval with a key the agent cannot reach, as attestations use.
      Not decided.
    - The default search folder is the folder that holds the run's own folder. For a ledger outside the
      `runs/<run_id>/` layout, that can be an unrelated folder, even one above the project root. `explore_runs_dir`
      makes this visible but does not prevent it.
    - The cross-check only sees explore runs that froze their answers (freezing is optional in explore mode) and whose
      ledgers are still in the runs directory. A deleted or never-frozen explore run is invisible. Git history of the
      runs directory is the backstop.
    - Without an explore freeze to compare against, the declaration lists live only in `.gdkit/project.yaml` until the
      freeze, and deleting them there hides them. After the freeze, removing one is detected like a changed answer.
    - "The same project" means "the same runs directory". A study that keeps explore runs elsewhere must pass
      `runs_dir` to `freeze_run`.
    - A declaration lets a change through. It does not judge whether the change was justified. That is the reviewer's
      call, using the model card.
    - Possible next step: make an explore freeze mandatory before any explore data action, so every explore run is
      visible to the cross-check. Not decided.

## Later

- **v1.1 (Should):** seed and determinism audit, checkpoint lineage, hardware and precision flags, cost budget, Croissant
  export, paper search skill, update check and `just-upgraded` marker, and guard tests across runs.
- **v2 (Later):** experiment tracker sync, licence and PII scan, automated incident matcher, external anchor for
  attestations.

## Known limits

See the list in `README.md`.
