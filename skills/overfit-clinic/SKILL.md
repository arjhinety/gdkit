---
name: overfit-clinic
description: Run the gdkit pre-study interview. Asks each question in chat, saves answers to .gdkit/project.yaml through the validator, and checks the result with gdkit doctor. Use before starting a study, or to resume one.
---

# /overfit-clinic

The clinic is the pre-study intake for gdkit. You ask the study questions in chat, one at a time. Each answer
is validated and saved by `gdkit.skill_api`. You never write `.gdkit/project.yaml` yourself.

## Rules

1. **Ask, never assume.** Ask each question exactly as the interview gives it, with its `why` line. Do not invent,
   suggest, or pick a value. If the user has not answered, the answer stays unanswered.
2. **Read back meaning-bearing answers before saving.** For `study_question`, `primary_metric`, `kill_criteria`,
   `baseline`, `split_protocol`, `general_capability_margin` and `refusal_threshold`, repeat the answer back in
   one line and wait for "yes". Thresholds and margins are pre-registrations. Save only what the user confirmed.
3. **Keep the user's words.** Save the reply exactly as typed. Write it to a file with the Write tool and pass it with
   `--value-file`, so punctuation cannot break the command.
4. **Mode.** Ask once at the start: explore (light, UNSET allowed) or registered (full preflight). Use the same mode for
   the whole session.
5. **Answers freeze when a run starts.** Say so at the end. `set` refuses to change an answer already in the file, with
   one exception: in registered mode, a required question left `UNSET` in explore mode is asked again. When `next` returns
   `replaces_unset: true` (the question was `UNSET` or skipped in explore mode), tell the user that this answer is being
   given after an explore run and will be listed under `replaced_unset` in the file and in the run's preregistration entry.
   When `promote` or `gdkit.cli attest` runs, it refuses a registered run whose answers differ from the frozen copy in its
   first ledger entry, and names the changed question. To change an answer after a run has started, the user starts a new
   run.
6. **Changing an answer before a run.** Only when the user explicitly asks to change an answer that is already in the file,
   use `change` (below). Say first that the change is declared under `changed_after_explore` and shown on the model card,
   and read the new answer back. Never use `change` or `declare` to fix a refusal on your own initiative. A registered
   freeze refuses any difference from an earlier explore run that is not declared, and prints a `declare` command for
   each one. Show the user each difference, explain that it may have been chosen after the explore run saw the data, and
   run a `declare` command only after the user confirms that one.
7. **Papers.** If `needs_paper_lookup` is yes, run `command -v pwc` and tell the user the result. If pwc is missing,
   say that lookups will be recorded as UNAVAILABLE until it is installed, and give the pwc project's install
   instructions. Do not guess the install command.
8. **Stop cleanly.** If the user stops partway, the answers so far are already saved. Say so, and say how to resume.

## Loop

Work from the study's project root, where `interview/questions.yaml` lives. The commands import `gdkit`. If it is
not installed, set `PYTHONPATH` to the gdkit checkout first, for example `PYTHONPATH=/path/to/gdkit`.

```bash
python -m gdkit.skill_api next --project .gdkit/project.yaml --mode <mode>
```

It prints one JSON object:
- `done: false` with `id`, `ask`, `why`, `type`, `values`, `required`, `replaces_unset`: a question to ask;
- `done: true` with `problems`: the interview is finished. Go to Finish.

For each question:
1. Ask it in chat. Show `values` if it is an enum, and say whether the answer is required for this mode.
2. Save the reply to a file, then run:
   ```bash
   python -m gdkit.skill_api set --project .gdkit/project.yaml --mode <mode> --id <id> --value-file <file>
   ```
   It prints `{"ok": true|false, "message": ...}`. On `false`, show the message and ask again. Do not force it.
3. If the user answers blank: in explore mode, run `set` with an empty value file. That records the skip under
   `skipped_in_explore`, and `next` moves on. In registered mode, save nothing. Ask again for a required question, and
   move on for an optional one.
4. Use UNSET only in explore mode, or for a question that is not required in registered mode. Registered mode refuses
   UNSET for required questions.
5. To change an answer the user explicitly asked to change (rule 6):
   ```bash
   python -m gdkit.skill_api change --project .gdkit/project.yaml --mode <mode> --id <id> --value-file <file>
   ```
6. To declare a difference the freeze named, after the user confirms it, run the command the freeze printed:
   ```bash
   python -m gdkit.skill_api declare --project .gdkit/project.yaml --id <id> --list replaced_unset|changed_after_explore
   ```

## Finish

```bash
python -m gdkit.doctor --project .gdkit/project.yaml --mode <mode>
```

- Exit 0: say which mode the answers are acceptable for, and that they freeze when a run starts.
- Exit 1: list the problems, then go back to the questions they belong to.

Report the result in plain words. Do not summarise an answer you did not read back to the user.
