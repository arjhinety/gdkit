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
5. **Answers freeze when a run starts.** Say so at the end. `set` refuses to change an answer already in the file. When
   `promote` or `gdkit.cli attest` runs, it refuses a registered run whose answers differ from the frozen copy in its first
   ledger entry, and names the changed question. To change an answer after a run has started, the user starts a new run.
6. **Papers.** If `needs_paper_lookup` is yes, run `command -v pwc` and tell the user the result. If pwc is missing,
   say that lookups will be recorded as UNAVAILABLE until it is installed, and give the pwc project's install
   instructions. Do not guess the install command.
7. **Stop cleanly.** If the user stops partway, the answers so far are already saved. Say so, and say how to resume.

## Loop

Work from the study's project root, where `interview/questions.yaml` lives. The commands import `gdkit`. If it is
not installed, set `PYTHONPATH` to the gdkit checkout first, for example `PYTHONPATH=/path/to/gdkit`.

```bash
python -m gdkit.skill_api next --project .gdkit/project.yaml --mode <mode>
```

It prints one JSON object:
- `done: false` with `id`, `ask`, `why`, `type`, `values`, `required`: a question to ask;
- `done: true` with `problems`: the interview is finished. Go to Finish.

For each question:
1. Ask it in chat. Show `values` if it is an enum, and say whether the answer is required for this mode.
2. Save the reply to a file, then run:
   ```bash
   python -m gdkit.skill_api set --project .gdkit/project.yaml --mode <mode> --id <id> --value-file <file>
   ```
   It prints `{"ok": true|false, "message": ...}`. On `false`, show the message and ask again. Do not force it.
3. If the user answers blank, do not save anything. For a required question in registered mode, ask again. For an
   optional question, move on.
4. Use UNSET only in explore mode, or for a question that is not required in registered mode. Registered mode refuses
   UNSET for required questions.

## Finish

```bash
python -m gdkit.doctor --project .gdkit/project.yaml --mode <mode>
```

- Exit 0: say which mode the answers are acceptable for, and that they freeze when a run starts.
- Exit 1: list the problems, then go back to the questions they belong to.

Report the result in plain words. Do not summarise an answer you did not read back to the user.
