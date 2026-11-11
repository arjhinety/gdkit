"""Upstream issue drafts: allow-listed fields, a check that refuses private details, amend, and no network."""

import getpass
import hashlib
import json
import platform
import socket
from pathlib import Path

import pytest

from gdkit.host import record_host
from gdkit.issues import DraftError, IssueLog, draft_issue, main
from gdkit.run import open_ledger, start_run


def _root(tmp_path):
    (tmp_path / ".gdkit").mkdir()
    (tmp_path / ".gdkit" / "project.yaml").write_text("study_question: q\n", encoding="utf-8")
    return tmp_path


def _log(root):
    return IssueLog(root / ".gdkit" / "issues" / "log.jsonl")


def _observe(root, slug="tf-keys", **kw):
    fields = dict(kind="upstream", severity="degrading", summary="Qwen3.5 linear attention keys are not loaded",
                  package="transformers", version="4.44.2", repo_url="https://github.com/huggingface/transformers",
                  expected="linear_attn.out_proj is loaded", actual="the key is skipped with a warning")
    fields.update(kw)
    return _log(root).observe(slug, project_root=root, **fields)


def _draft(root, slug="tf-keys", **kw):
    return draft_issue(_log(root), slug, project_root=root, **kw)


# --- a clean draft ---------------------------------------------------------------------------


def test_a_clean_draft_is_written_recorded_and_git_ignored(tmp_path):
    root = _root(tmp_path)
    _observe(root)
    result = _draft(root)
    path = root / result["path"]
    text = path.read_text(encoding="utf-8")
    assert result["path"] == ".gdkit/issues/drafts/tf-keys.md"
    assert text.startswith("# Qwen3.5 linear attention keys are not loaded\n")
    for expected in ("transformers 4.44.2", "linear_attn.out_proj is loaded", "the key is skipped with a warning"):
        assert expected in text
    assert result["new_issue_url"] == "https://github.com/huggingface/transformers/issues/new"
    issue = _log(root).issues()["tf-keys"]
    assert issue["status"] == "drafted"
    assert issue["draft"] == {"path": ".gdkit/issues/drafts/tf-keys.md",
                              "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    assert (root / ".gdkit" / "issues" / "drafts" / ".gitignore").read_text(encoding="utf-8") == "*\n"


def test_the_environment_comes_from_the_linked_runs_host_summary(tmp_path):
    root = _root(tmp_path)
    run = start_run(root / "runs", "r1", mode="registered", project_path=root / ".gdkit" / "project.yaml")
    summary = record_host(open_ledger(run), run)["params"]["summary"]
    _observe(root, run_dir=run)
    text = (root / _draft(root)["path"]).read_text(encoding="utf-8")
    assert f"Python {summary['python']['version']} ({summary['python']['implementation']})" in text
    assert f"{summary['platform']['system']} {summary['platform']['machine']}" in text


def test_only_allow_listed_fields_reach_the_draft(tmp_path):
    root = _root(tmp_path)
    (root / "trace.txt").write_text("trace\n", encoding="utf-8")
    _observe(root, evidence=["trace.txt"], error="KeyError: linear_attn", affected_gates=["direction_derivation"])
    _log(root).workaround("tf-keys", description="accept both names in our private harness", commit="196b6f2")
    text = (root / _draft(root)["path"]).read_text(encoding="utf-8")
    issue = _log(root).issues()["tf-keys"]
    for private in ("trace.txt", "direction_derivation", "private harness", "196b6f2", issue["fingerprint"]):
        assert private not in text, private


def test_the_reproducer_file_is_included(tmp_path):
    root = _root(tmp_path)
    (root / "repro.py").write_text("from transformers import AutoModel\nAutoModel.from_pretrained('x')\n",
                                   encoding="utf-8")
    _observe(root, reproducer="repro.py")
    text = (root / _draft(root)["path"]).read_text(encoding="utf-8")
    assert "```python\nfrom transformers import AutoModel\nAutoModel.from_pretrained('x')\n```" in text


def test_a_40_hex_git_sha_is_allowed(tmp_path):
    root = _root(tmp_path)
    _observe(root, actual="broken since commit 196b6f2d674daa058e3b5e409e4c18fbd3114edb upstream")
    _draft(root)


def test_a_non_github_repo_gets_no_new_issue_url(tmp_path):
    root = _root(tmp_path)
    _observe(root, repo_url="https://gitlab.example.org/team/lib")
    assert _draft(root)["new_issue_url"] is None


# --- the check refuses private details -------------------------------------------------------


@pytest.mark.parametrize(
    "field, value, rule",
    [
        ("actual", "fails in run r-secret-7 at step 3", "run id"),
        ("actual", "hash " + "ab" * 32, "64-character hex"),
        ("actual", "see /home/alice/study/x.py", "home path"),
        ("actual", "see C:\\Users\\bob\\study\\x.py", "home path"),
        ("actual", "read runs/r1/splits/train.jsonl", "runs/ path"),
        ("summary", "crash on " + platform.node(), "hostname"),
        ("expected", "works for " + getpass.getuser(), "username"),
    ],
)
def test_the_check_refuses_private_details_and_writes_nothing(tmp_path, field, value, rule):
    root = _root(tmp_path)
    (root / "runs" / "r-secret-7").mkdir(parents=True)
    _observe(root, **{field: value})
    head = _log(root).head
    with pytest.raises(DraftError, match=rule):
        _draft(root)
    assert not (root / ".gdkit" / "issues" / "drafts" / "tf-keys.md").exists()
    assert _log(root).head == head  # nothing recorded


def test_the_check_reads_the_reproducer_and_project_deny_patterns(tmp_path):
    root = _root(tmp_path)
    (root / "repro.py").write_text("token = 'sk-live-123'\n", encoding="utf-8")
    (root / ".gdkit" / "issues").mkdir(parents=True)
    (root / ".gdkit" / "issues" / "deny.txt").write_text("# secrets\nsk-live-\\d+\n", encoding="utf-8")
    _observe(root, reproducer="repro.py")
    with pytest.raises(DraftError, match=r"deny pattern sk-live-\\d\+"):
        _draft(root)


def test_amend_is_the_way_out_of_a_refusal_end_to_end(tmp_path, capsys):
    root = _root(tmp_path)
    log_path = root / ".gdkit" / "issues" / "log.jsonl"
    _observe(root, actual="see /home/alice/study/x.py")
    args = ["--log", str(log_path)]
    assert main([*args, "draft", "tf-keys", "--project-root", str(root)]) == 1
    refused = json.loads(capsys.readouterr().out)
    assert "home path" in refused["message"] and "amend" in refused["message"]
    assert main([*args, "amend", "tf-keys", "--actual", "the key is skipped with a warning"]) == 0
    capsys.readouterr()
    assert main([*args, "draft", "tf-keys", "--project-root", str(root)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["path"] == ".gdkit/issues/drafts/tf-keys.md" and out["new_issue_url"].endswith("/issues/new")
    assert _log(root).issues()["tf-keys"]["actual"] == "the key is skipped with a warning"


def test_drafting_opens_no_network_connection(tmp_path, monkeypatch):
    root = _root(tmp_path)
    _observe(root)

    def no_network(*args, **kwargs):
        raise AssertionError("gdkit tried to open a network socket")

    monkeypatch.setattr(socket, "socket", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    _draft(root)


# --- triage is its own flag ------------------------------------------------------------------


@pytest.mark.parametrize("event", ["filed", "drafted"])
def test_filing_or_drafting_does_not_count_as_triage(tmp_path, event):
    from gdkit.issues import blocking_reason

    root = _root(tmp_path)
    _observe(root)  # degrading, untriaged
    if event == "filed":
        _log(root).filed("tf-keys", url="https://github.com/huggingface/transformers/issues/1")
    else:
        _draft(root)
    assert blocking_reason(_log(root).issues()["tf-keys"]) == "degrading and not triaged"
    _log(root).triage("tf-keys")
    assert blocking_reason(_log(root).issues()["tf-keys"]) is None
