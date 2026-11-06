import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from gates_fixture import COUNT, REGISTRY, TRAIN
from prereg_fixture import ANSWERS, freeze, proj, write_project
from gdkit.gates import run_gate
from gdkit.interview import UNSET, load_questions
from gdkit.ledger import Ledger
from gdkit.prereg import PREREG_OP, PreregError, answers_digest, check_answers, freeze_run
from gdkit.promote import PromoteError, attestable_hash, promote

QUESTIONS = load_questions(Path(__file__).resolve().parents[1] / "interview" / "questions.yaml")
ACCEPT = lambda run_id, digest: True  # noqa: E731
H = "a" * 64


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def frozen_run(tmp_path):
    """A registered run: frozen answers, a load entry, and receipts for both gates."""
    run = tmp_path / "run"
    run.mkdir()
    data = run / "data.csv"
    data.write_text("a\nb\nc\n", encoding="utf-8")
    ledger = Ledger(run / "ledger.jsonl", run_id="run-1", mode="registered")
    freeze(ledger, run)
    ledger.record("load", rows_before=0, rows_after=3, input_hash=_sha(data), output_hash=_sha(data))
    head = json.loads((run / "ledger.jsonl").read_text(encoding="utf-8").splitlines()[-1])["entry_hash"]
    run_gate(run, COUNT, inputs=["data.csv"], run_id="run-1", ledger_head=head)
    run_gate(run, TRAIN, inputs=["data.csv"], run_id="run-1", ledger_head=head)
    return run


def entries(run):
    return [json.loads(line) for line in (run / "ledger.jsonl").read_text(encoding="utf-8").splitlines()]


def test_the_freeze_is_the_first_entry_and_holds_the_answers(tmp_path):
    run = frozen_run(tmp_path)
    first = entries(run)[0]
    assert first["op"] == PREREG_OP
    assert first["params"]["answers"] == ANSWERS
    assert first["params"]["answers_sha256"] == answers_digest(ANSWERS)
    assert first["input_hash"] == first["output_hash"] == answers_digest(ANSWERS)


def test_an_explore_freeze_records_the_answers_without_content_hashes(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    write_project(run)
    ledger = Ledger(run / "ledger.jsonl", run_id="run-1", mode="explore")
    entry = freeze_run(ledger, proj(run))
    assert entry["input_hash"] is None and entry["output_hash"] is None
    assert entry["params"]["answers"] == ANSWERS


def test_a_freeze_after_a_data_entry_is_refused(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    write_project(run)
    ledger = Ledger(run / "ledger.jsonl", run_id="run-1", mode="explore")
    ledger.record("load", rows_before=0, rows_after=3, boundary=True, input_hash=H, output_hash=H)
    with pytest.raises(PreregError, match="before the first data action"):
        freeze_run(ledger, proj(run))


def test_a_freeze_without_a_project_file_is_refused(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    ledger = Ledger(run / "ledger.jsonl", run_id="run-1", mode="registered")
    with pytest.raises(PreregError, match="does not exist"):
        freeze_run(ledger, tmp_path / "missing.yaml")


def test_a_freeze_refuses_answers_the_interview_does_not_accept(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    write_project(run, {"study_question": UNSET})
    ledger = Ledger(run / "ledger.jsonl", run_id="run-1", mode="registered")
    with pytest.raises(PreregError, match="not acceptable to freeze"):
        freeze_run(ledger, proj(run), questions_data=QUESTIONS)


def test_promote_accepts_a_run_whose_answers_are_unchanged(tmp_path):
    run = frozen_run(tmp_path)
    report = promote(run, REGISTRY, ["row_count", "train"], attestation=ACCEPT, project_path=proj(run))
    assert report["gates"]


def test_promote_refuses_an_answer_changed_after_the_run_started_and_names_it(tmp_path):
    run = frozen_run(tmp_path)
    changed = dict(ANSWERS, baseline="a new, easier baseline")
    proj(run).write_text(yaml.safe_dump(changed), encoding="utf-8")
    with pytest.raises(PromoteError, match="answers changed since the run started: baseline"):
        promote(run, REGISTRY, ["row_count", "train"], attestation=ACCEPT, project_path=proj(run))


def test_promote_refuses_an_added_or_removed_answer(tmp_path):
    run = frozen_run(tmp_path)
    proj(run).write_text(yaml.safe_dump({"study_question": ANSWERS["study_question"]}), encoding="utf-8")
    with pytest.raises(PromoteError, match="baseline"):
        promote(run, REGISTRY, ["row_count", "train"], attestation=ACCEPT, project_path=proj(run))


def test_a_rewritten_freeze_breaks_the_ledger_chain(tmp_path):
    run = frozen_run(tmp_path)
    lines = (run / "ledger.jsonl").read_text(encoding="utf-8").splitlines()
    entry = json.loads(lines[0])
    entry["params"]["answers"]["baseline"] = "a new, easier baseline"
    entry["params"]["answers_sha256"] = answers_digest(entry["params"]["answers"])
    lines[0] = json.dumps(entry, sort_keys=True, separators=(",", ":"))
    (run / "ledger.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    proj(run).write_text(yaml.safe_dump(dict(ANSWERS, baseline="a new, easier baseline")), encoding="utf-8")
    with pytest.raises(PromoteError, match="does not verify"):
        promote(run, REGISTRY, ["row_count", "train"], attestation=ACCEPT, project_path=proj(run))


def test_a_run_with_no_freeze_is_refused(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    write_project(run)
    ledger = Ledger(run / "ledger.jsonl", run_id="run-1", mode="registered")
    ledger.record("load", rows_before=0, rows_after=3, input_hash=H, output_hash=H)
    problems = check_answers(entries(run), proj(run))
    assert problems and "no preregistration entry" in problems[0]


def test_attestation_refuses_an_answer_changed_after_the_run_started(tmp_path):
    run = frozen_run(tmp_path)
    proj(run).write_text(yaml.safe_dump(dict(ANSWERS, baseline="moved")), encoding="utf-8")
    with pytest.raises(PromoteError, match="answers changed since the run started: baseline"):
        attestable_hash(run, "train", REGISTRY, project_path=proj(run))


def test_the_cli_attest_refuses_a_changed_answer(tmp_path):
    import os

    run = frozen_run(tmp_path)
    proj(run).write_text(yaml.safe_dump(dict(ANSWERS, baseline="moved")), encoding="utf-8")
    root = Path(__file__).resolve().parents[1]
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(root), str(root / "tests"), str(root / "vendor" / "opengrad")])}
    result = subprocess.run(
        [sys.executable, "-m", "gdkit.cli", "--registry", "gates_fixture:REGISTRY", "attest", "--project", str(proj(run)),
         str(run), "train"],
        capture_output=True, text=True, env=env, cwd=root,
    )
    assert result.returncode == 1
    assert "answers changed since the run started" in result.stderr
