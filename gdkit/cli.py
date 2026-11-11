"""Command line for the parts that CI calls.

    python -m gdkit.cli --registry package.module:REGISTRY attest RUN_DIR GATE

``attest`` validates one expensive gate's receipt (the binding and input checks, not the gate
re-run) and prints the receipt hash on stdout. The attestation workflow signs that hash into a tag.

    python -m gdkit.cli --registry package.module:REGISTRY issues-head --project .gdkit/project.yaml

``issues-head`` prints the project issue log's head (``none`` without a log) after checking that the log verifies.
The workflow signs it into the tag's ``issues-head:`` line.
Exit status is 0 on success and 1 on refusal, with the reason on stderr.
"""

from __future__ import annotations

import argparse
import importlib
import sys

from gdkit.promote import PromoteError, attestable_hash


def load_registry(spec: str) -> dict:
    module_name, sep, attribute = spec.partition(":")
    if not sep or not module_name or not attribute:
        raise SystemExit("--registry must look like package.module:ATTRIBUTE")
    return getattr(importlib.import_module(module_name), attribute)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="gdkit")
    parser.add_argument("--registry", required=True, help="module:attribute holding the gate registry dict")
    commands = parser.add_subparsers(dest="command", required=True)
    attest = commands.add_parser("attest", help="validate one expensive receipt and print its hash")
    attest.add_argument("run_dir")
    attest.add_argument("gate")
    attest.add_argument("--project", default=".gdkit/project.yaml", help="project file the run was frozen from")
    head = commands.add_parser("issues-head", help="print the project issue log head, or none")
    head.add_argument("--project", default=".gdkit/project.yaml")
    args = parser.parse_args(argv)

    if args.command == "issues-head":
        from gdkit.issues import IssueError, IssueLog
        from gdkit.promote import default_issues_path

        path = default_issues_path(args.project)
        try:
            print(IssueLog(path).head or "none" if path.exists() else "none")
        except IssueError as exc:
            print(f"refused: {exc}", file=sys.stderr)
            return 1
        return 0

    registry = load_registry(args.registry)
    try:
        digest = attestable_hash(args.run_dir, args.gate, registry, project_path=args.project)
    except PromoteError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    print(digest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
