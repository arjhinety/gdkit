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
  the input file hashes. A gate's check must return a dict with a boolean `passed` field, for example
  `{"passed": True, "rows": 3}`. Any other result is refused and no receipt is written. Gate names may use only letters,
  digits, `_` and `-`. Each receipt carries `receipt_schema` (now 1). With `capture_log=True`, gdkit captures
  everything the check writes to stdout and stderr at the file-descriptor level, subprocesses included, and hashes the
  log into the receipt. While the gate runs the log is `logs/<gate>.log.partial`, so you can watch it with `tail -f` or
  `Get-Content -Wait`. When the check returns or raises it becomes `logs/<gate>.log`. On Windows a reader that opened
  the partial file without delete sharing (a plain Python `open`) blocks the rename; gdkit then copies it instead and
  records `"finalised": "copied"` and whether the partial file was left behind (`partial_left`). The receipt also records the platform
  and locale encoding, because the log keeps the bytes as written (on Windows, CRLF and the locale encoding from
  child processes and earlier handlers, UTF-8 from `print`).
- **Old receipts.** A receipt without `receipt_schema` (gdkit before this field) is schema 0. `gdkit.audit.verify_run`
  checks it under the rules it was written with: no pass flag, tag `attest/<run_id>`. `promote` refuses it, because a
  failing receipt written today could otherwise drop the field to get the older rules. Re-run the gate to promote.
- **Run recording.** `python -m gdkit.run start` creates `runs/<run_id>/`, freezes the answers, and records
  `run.json`. `host`, `scope`, `config`, `decide` and `record` write each side file as exact bytes and record its
  hash as an event in the ledger. The full host record stays in a git-ignored `host.json`; the ledger gets its hash
  and a summary without hostname, username or paths. Record everything before the gates: a receipt binds the ledger
  head, so a later entry makes it stale. `python -m gdkit.run audit RUN_DIR` prints the `verify_run` report.
- **Issues.** `python -m gdkit.issues` keeps a project-wide, hash-chained log of problems found while running
  (`observe`, `again`, `triage`, `workaround`, `filed`, `resolve`). Issues link to a run by its ledger head without
  writing to it. `promote` and `attest` refuse while an unresolved issue that affects the run is blocking with no
  workaround, or degrading and untriaged. Anyone can append the event that clears it, so this is a visible record,
  not enforcement. gdkit never contacts an upstream repo; `filed` records the URL a person filed.
- **Upstream drafts.** `python -m gdkit.issues draft SLUG` writes `.gdkit/issues/drafts/<slug>.md` (git-ignored)
  from allow-listed fields only: summary, package and version, Python and OS from the linked run's host summary,
  what happened, what was expected, and the reproducer file. It refuses the draft, writing and recording nothing,
  if the text holds a 64-character hex hash, a home path, a `runs/` path, a known run ID, this machine's or the
  run's username or hostname, or a pattern from `.gdkit/issues/deny.txt`. `amend` rewrites the issue's text. A clean
  draft is recorded by hash and gdkit prints the GitHub new-issue URL; filing it is up to you.
- **Promotion.** `promote()` checks each required receipt, refuses any whose result is not `passed: true`, re-runs cheap
  gates, and requires an SSH-signed attestation for expensive ones. Each receipt has its own tag,
  `attest/<run_id>/<gate>/<first 16 hex of the receipt hash>`, so a re-run gate can be approved again. The tag's
  subject is exactly the receipt hash and its body is one line, `issues-head: <issue log head | none>`. `promote`
  requires that signed head to still be an entry of the issue log, so rewriting or deleting the issues the reviewer
  saw is caught. It refuses explore runs.
- **Census.** A gate can return `{"passed": ..., "census": Census(...).to_dict()}` (`gdkit.census`): what it
  discovered, passed, failed, skipped and blocked. Every discovered item needs exactly one outcome, every failure,
  skip and block a reason, and a required population may not be empty. `run_gate` and `promote` refuse a census that
  breaks these rules or a `passed: true` result whose census has failures.
- **Same machine.** When a run has a recorded host, `run_gate` refuses to run a gate on a machine whose host summary
  differs (Python, OS, recorded package versions, probe output), naming the field. `check_host=False` turns it off,
  and the receipt's `host_check` says which. `promote` does not check the host, since CI is another machine.
- **Append-only.** `python -m gdkit.append_only --base SHA` checks that run ledgers and the issue log only grew
  between two commits, that no run file was deleted and `run.json` did not change, and that any rewritten receipt or
  side file is accounted for by the ledger or a receipt. `.github/workflows/append-only.yml` runs it on push and
  pull requests; copy it into a study repository and make it a required check for it to count as enforcement.
- **Pre-registration.** The first ledger entry of a registered run holds the study's answers. `promote` and `attest` refuse
  a run whose answers differ from that frozen copy, and they name the changed answer.
- **Answers given after an explore run.** An answer given after an explore run may have been chosen after seeing the
  data, so it must be declared. A question left `UNSET` or skipped in explore mode and answered later is listed under
  `replaced_unset`. A real answer can only be changed with `skill_api change`, which lists it under
  `changed_after_explore`. A registered freeze compares the answers with every explore-mode freeze in the same runs
  directory and refuses any undeclared difference. Declared ones are frozen with the run and shown on the model card.
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
python -m gdkit.skill_api change --project .gdkit/project.yaml --questions interview/questions.yaml --mode registered --id QID --value-file FILE
python -m gdkit.skill_api declare --project .gdkit/project.yaml --questions interview/questions.yaml --id QID --list replaced_unset
```

The non-interactive backend the skill uses. Each prints one JSON object. `change` replaces a real answer and declares
it under `changed_after_explore`. `declare` adds a question to `replaced_unset` or `changed_after_explore` without
changing its answer. Use it for a question never asked during an earlier explore run, or an answer dropped since then.
The registered freeze prints the exact command for each difference it refuses.

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
- A gate's code hash covers only its check function. A change to a helper the check calls does not mark old receipts
  as stale.
- The ledger refuses to append when the file changed since its last write, so a second writer is detected. This is not
  a lock: two writers in the same instant can both pass the check.
- The explore cross-check only sees explore runs that froze their answers and whose ledgers are still in the runs
  directory. Without one, the declaration lists live only in `.gdkit/project.yaml` before the freeze, where deleting
  them hides them. See the explore-mode item in `ROADMAP.md`.
- The same user can still bypass some checks. See `ROADMAP.md`.
- A captured gate log holds the bytes as written, so line endings differ by OS (CRLF on Windows from the console
  streams and child processes). The same check gives a different log hash on Windows and Linux.
- The same-machine check means a run's environment must stay frozen: upgrading a recorded package (gdkit included)
  mid-run makes later gates refuse, and re-recording the host adds a ledger entry that makes earlier receipts stale.
  Upgrade by starting a new run, and leave out probes whose output changes (temperature, memory in use).
- The append-only check compares commits; it does not stop a force-push that rewrites history. That needs branch
  protection.
- The append-only check accepts a rewritten side file whose new hash appears anywhere in the run's ledger or
  receipts, not only in a newly added entry.
- An attestation signed with `issues-head: none` passes even if an issue log existed but had not been committed.
- `check_host=False` is visible only in the receipt's `host_check`. `verify_run` does not apply the census rules.
- Issue blocks are cleared by events anyone can append (a workaround, a triage, a resolution). Nothing checks who
  appended them.

See `ROADMAP.md` for status and planned work.

## Status

Pre-1.0. File formats and function names may change. See `CHANGELOG.md`.

## Licence

Apache-2.0. See `LICENSE` and `NOTICE`.
