# gdkit

gdkit (gradient kit) is a provenance and preflight kit for machine learning research runs. It records what a run did in a
tamper-evident ledger, freezes the study's pre-registered answers before the data is touched, and refuses to promote a
result unless its receipts check out.

gdkit does not depend on OpenGrad at run time. It was written from OpenGrad's workflow, and one OpenGrad hashing module is
vendored under `vendor/` (see NOTICE and SOURCE.lock).

## What it does

- **Ledger.** An append-only JSON Lines file. Each entry carries `prev_hash` and `entry_hash`, is fsynced when written, and
  `verify()` checks the chain. It detects accidental change. It does not stop someone who can rewrite the whole file.
- **Modes.** `explore` allows unset answers and records content hashes only at boundaries. `registered` needs content
  hashes on every entry and no unset required answers.
- **Gate receipts.** `run_gate` writes an unsigned receipt bound to the run id, the ledger head, the gate's source hash and
  the input file hashes.
- **Promotion.** `promote()` checks each required receipt, re-runs cheap gates, and requires an SSH-signed attestation for
  expensive ones. It refuses explore runs.
- **Pre-registration.** The first ledger entry of a registered run holds the study's answers. `promote` and `attest` refuse
  a run whose answers differ from that frozen copy, and they name the changed answer.
- **Paper lookups.** Papers With Code lookups go through the `pwc` command. Each lookup is recorded. A failed lookup is
  recorded as UNAVAILABLE and blocks promotion until a later lookup for the same request succeeds.
- **Cards.** Dataset and model cards are generated from the ledger, and `audit_card` checks a card against it.
- **Interview.** `/overfit-clinic` asks the study's questions one at a time, validates each answer, and saves it to
  `.gdkit/project.yaml`.

## Install

```
python -m pip install -e ".[test]"
```

Python 3.11 or newer. The only runtime dependency is PyYAML.

## Test

```
python -m pytest
```

The SSH attestation tests need `git` and `ssh-keygen`, and they skip when either is missing.

## Command line

```
python -m gdkit.doctor --project .gdkit/project.yaml --questions interview/questions.yaml --mode registered
```

Checks the project answers against the interview. Exit 0 when they are acceptable for the mode.

```
python -m gdkit.skill_api next --project .gdkit/project.yaml --questions interview/questions.yaml --mode registered
python -m gdkit.skill_api set  --project .gdkit/project.yaml --questions interview/questions.yaml --mode registered --id QID --value-file FILE
```

The non-interactive backend the skill uses. Each prints one JSON object.

```
python -m gdkit.cli --registry package.module:REGISTRY attest RUN_DIR GATE --project .gdkit/project.yaml
```

Validates one expensive gate's receipt and prints its receipt hash. The attestation workflow signs that hash.

`promote()` is a Python function in `gdkit.promote`. It has no command line yet.

## Using the interview skill

Copy `skills/overfit-clinic/` into `~/.claude/skills/`, then run `/overfit-clinic` from a study's project root that contains
`interview/questions.yaml`. If gdkit is not installed, set `PYTHONPATH` to this checkout.

## Known limits

- The ledger detects accidental change to its own file. It does not stop someone who can rewrite the whole file.
- A receipt check is a convention for shell commands. `promote` catches a forged receipt through the hash, the re-run and the
  attestation checks, not by preventing the write.
- An attestation shows that the owner approved a committed receipt. It does not show that the expensive job ran.
- The interview checks answer structure, not meaning. A vague study question passes validation.
- The attestation workflow has not run on GitHub yet.
- The same user can still bypass some checks. See `ROADMAP.md`.

See `ROADMAP.md` for status and planned work.

## Status

Pre-1.0. File formats and function names may change. See `CHANGELOG.md`.

## Licence

Apache-2.0. See `LICENSE` and `NOTICE`.
