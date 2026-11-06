# Security policy

## Reporting a vulnerability

Please do not open a public issue for a security problem. Report it privately through GitHub's
"Report a vulnerability" button on this repository's Security tab. If that button is missing, private
reporting has not been enabled yet. Open a public issue that says only "security contact requested", with
no details.

Useful reports name the file, the command and the input, and say what a forged or altered input was able to do.
Examples in scope:

- a receipt, ledger entry or frozen answer that `promote` or `gdkit.cli attest` accepts when it should refuse;
- a path in `skills/` or `.github/workflows/` that lets untrusted input run shell commands;
- a way to make the attestation workflow sign something it should not sign.

## Known limits are not vulnerabilities by themselves

`ROADMAP.md` and the README list limits that are known and deliberate. For example, the ledger detects accidental
change but not a full rewrite of the file by someone with write access. A report that shows one of these limits
failing in a new way is welcome.
