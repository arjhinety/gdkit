"""Stand-in for the pwc CLI, so the tests never touch the network.

Behaviour is chosen by the first argument:
  version           -> the version line the real CLI prints
  search ...        -> a search response shaped like the live one (schema v1)
  fail ...          -> exit status 2 with a message on stderr
  garbage ...       -> a response that is not JSON
  jsonlist ...      -> valid JSON that is a list, not an object
  badresults ...    -> an object whose results are not a list of objects
"""

import json
import sys

args = sys.argv[1:]
if not args:
    sys.exit(64)
mode = args[0]
if mode == "version":
    print("pwc 0.4.2-test\tapi v1")
elif mode == "search":
    print(
        json.dumps(
            {
                "schema_version": "v1",
                "data": {
                    "next_page": None,
                    "results": [
                        {
                            "id": "26170",
                            "arxiv_id": "2401.12503",
                            "title": "Small Language Model Meets with Reinforced Vision Vocabulary",
                            "published": "2024-01-23",
                            "citation_count": 52,
                            "has_official_implementation": False,
                            "code_repository_count": 0,
                        }
                    ],
                },
            }
        )
    )
elif mode == "fail":
    print("network error", file=sys.stderr)
    sys.exit(2)
elif mode == "flaky":
    # Fails once, then succeeds. The marker file in FAKE_PWC_MARKER records the first failure.
    import os

    marker = os.environ.get("FAKE_PWC_MARKER", "")
    if marker and os.path.exists(marker):
        print(json.dumps({"schema_version": "v1", "data": {"results": [
            {"id": "26170", "arxiv_id": "2401.12503", "title": "T", "published": "2024-01-23",
             "citation_count": 52, "has_official_implementation": False, "code_repository_count": 0}]}}))
    else:
        if marker:
            open(marker, "w").close()
        print("temporary outage", file=sys.stderr)
        sys.exit(2)
elif mode == "jsonlist":
    print("[1, 2]")
elif mode == "badresults":
    print(json.dumps({"schema_version": "v1", "data": {"results": [1, "two"]}}))
elif mode == "garbage":
    print("<html>not json</html>")
else:
    sys.exit(64)
