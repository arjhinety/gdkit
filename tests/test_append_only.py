"""The append-only check, on throwaway git repositories."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from gdkit.append_only import AppendOnlyError, check, main

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is needed")


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                           "-c", "core.autocrlf=false", *args], check=True, capture_output=True, text=True).stdout.strip()


def _write(repo, rel, data):
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))


def _commit(repo, message="c"):
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "--allow-empty", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _entry(seq, **params):
    return json.dumps({"seq": seq, "entry_hash": f"{seq:064x}", "params": params}) + "\n"


@pytest.fixture
def repo(tmp_path):
    repo = tmp_path / "study"
    repo.mkdir()
    _git(repo, "init", "-q")
    _write(repo, "runs/r1/ledger.jsonl", _entry(0))
    _write(repo, "runs/r1/run.json", '{"run_id": "r1"}\n')
    _write(repo, "runs/r1/config.json", '{"lr": 1}\n')
    _write(repo, "runs/r1/receipts/g.json", json.dumps({"ledger_head": f"{0:064x}"}))
    _write(repo, ".gdkit/issues/log.jsonl", '{"seq":0}\n')
    _write(repo, "README.md", "study\n")
    return repo


def _sha(text):
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_appending_to_ledgers_and_adding_files_is_clean(repo):
    base = _commit(repo)
    _write(repo, "runs/r1/ledger.jsonl", _entry(0) + _entry(1))
    _write(repo, ".gdkit/issues/log.jsonl", '{"seq":0}\n{"seq":1}\n')
    _write(repo, "runs/r2/ledger.jsonl", _entry(0))
    report = check(repo, base, _commit(repo))
    assert report["ok"] and report["examined"] == 3


def test_files_outside_runs_are_not_examined(repo):
    base = _commit(repo)
    _write(repo, "README.md", "changed\n")
    report = check(repo, base, _commit(repo))
    assert report == {**report, "ok": True, "examined": 0}


@pytest.mark.parametrize("ledger", ["runs/r1/ledger.jsonl", ".gdkit/issues/log.jsonl"])
def test_an_edited_ledger_is_a_violation(repo, ledger):
    base = _commit(repo)
    _write(repo, ledger, "{\"seq\": 0, \"edited\": true}\n")
    report = check(repo, base, _commit(repo))
    assert not report["ok"] and "byte-prefix" in report["violations"][0]


def test_deleting_or_moving_a_ledger_is_a_violation(repo):
    base = _commit(repo)
    (repo / "runs/r1/ledger.jsonl").rename(repo / "runs/r1/ledger.old")
    report = check(repo, base, _commit(repo))
    assert any("runs/r1/ledger.jsonl: deleted" in v for v in report["violations"])


def test_run_json_may_not_change(repo):
    base = _commit(repo)
    _write(repo, "runs/r1/run.json", '{"run_id": "r1", "label": "late"}\n')
    assert "run.json may not change" in check(repo, base, _commit(repo))["violations"][0]


def test_a_rewritten_side_file_needs_its_new_hash_in_the_record(repo):
    base = _commit(repo)
    _write(repo, "runs/r1/config.json", '{"lr": 2}\n')
    head = _commit(repo)
    assert "no ledger entry or receipt" in check(repo, base, head)["violations"][0]
    _write(repo, "runs/r1/ledger.jsonl", _entry(0) + _entry(1, path="config.json", sha256=_sha('{"lr": 2}\n')))
    assert check(repo, base, _commit(repo))["ok"]


def test_a_rewritten_receipt_must_name_an_entry_of_the_ledger(repo):
    base = _commit(repo)
    _write(repo, "runs/r1/receipts/g.json", json.dumps({"ledger_head": "f" * 64}))
    assert "ledger_head is not an entry" in check(repo, base, _commit(repo))["violations"][0]
    _write(repo, "runs/r1/ledger.jsonl", _entry(0) + _entry(1))
    _write(repo, "runs/r1/receipts/g.json", json.dumps({"ledger_head": f"{1:064x}"}))
    assert check(repo, base, _commit(repo))["ok"]


def test_a_crlf_rewrite_of_a_recorded_file_is_still_accounted_for(repo):
    base = _commit(repo)
    _write(repo, "runs/r1/config.json", b'{"lr": 2}\r\n')
    _write(repo, "runs/r1/ledger.jsonl", _entry(0) + _entry(1, sha256=_sha('{"lr": 2}\n')))
    assert check(repo, base, _commit(repo))["ok"]


@pytest.mark.parametrize("name", ["données é.json", "with space.json", 'quote".json'])
def test_paths_git_would_quote_are_still_examined(repo, name):
    if '"' in name and __import__("sys").platform == "win32":
        pytest.skip("Windows file names cannot contain a double quote")
    _write(repo, f"runs/r1/{name}", "x\n")
    base = _commit(repo)
    (repo / "runs" / "r1" / name).unlink()
    report = check(repo, base, _commit(repo))
    assert report["examined"] == 1 and report["violations"] == [f"runs/r1/{name}: deleted; run records may not be deleted"]


@pytest.mark.parametrize("base", ["0" * 40, "1234567890abcdef1234567890abcdef12345678"])
def test_an_unusable_base_is_an_error_not_a_pass(repo, base, capsys):
    _commit(repo)
    with pytest.raises(AppendOnlyError):
        check(repo, base)
    assert main(["--repo", str(repo), "--base", base]) == 2
    assert json.loads(capsys.readouterr().out)["ok"] is False


def test_the_cli_reports_examined_and_exits_1_on_a_violation(repo, capsys):
    base = _commit(repo)
    (repo / "runs/r1/config.json").unlink()
    _commit(repo)
    assert main(["--repo", str(repo), "--base", base]) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["examined"] == 1 and "deleted" in out["violations"][0]
