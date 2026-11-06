# Contributing

gdkit is small and strict on purpose: it refuses things, and a change that makes it accept more needs a good reason.

## Set up

```
python -m pip install -e ".[test]"
python -m pytest
```

Python 3.11 or newer. The SSH attestation tests need `git` and `ssh-keygen`, and they skip when either is missing.

## Rules for a change

- Keep the test suite green. Add a test for each new refusal and each bug fix.
- Do not edit files under `vendor/` by hand. They are pinned by hash in `SOURCE.lock`. To update one, copy the new
  upstream file, update the hash and commit in `SOURCE.lock`, and say so in the commit message.
- If you change a rule in a document (README, ROADMAP, the skill), change the code or the test that enforces it in the
  same commit. Numeric claims in prose must match the code's units and formulas.
- Add a line to `CHANGELOG.md` under "Unreleased".

## Commits and releases

Commit messages start with a short type: `feat`, `fix`, `docs`, `test`, `refactor`, `ci` or `chore`, then a short
imperative summary.

Releases are tagged `vX.Y.Z` and match `gdkit.__version__`. Tags named `attest/...` are reserved for attestations made by
the attestation workflow.

## Licence

By contributing you agree that your contribution is licensed under Apache-2.0, the same as the project.
