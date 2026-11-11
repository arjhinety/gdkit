"""Gate registry used by the promote tests and by the CLI tests.

The CLI loads this registry by name (``gates_fixture:REGISTRY``), so the receipts the tests write
and the gates the CLI runs share one set of functions and therefore one set of code hashes.
"""

from pathlib import Path

from gdkit.gates import Gate


def count_rows(run_dir):
    return {"passed": True, "rows": len((Path(run_dir) / "data.csv").read_text(encoding="utf-8").splitlines())}


def fake_train(run_dir):
    return {"passed": True, "loss": 0.5}


COUNT = Gate("row_count", cheap=True, check=count_rows)
TRAIN = Gate("train", cheap=False, check=fake_train)
REGISTRY = {"row_count": COUNT, "train": TRAIN}


def failing_leak_check(run_dir):
    return {"passed": False, "leak_rows": 37}
