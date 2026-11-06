"""Shared test setup for runs that must carry a pre-registration freeze."""

from pathlib import Path

import yaml

from gdkit.ledger import Ledger
from gdkit.prereg import freeze_run

ANSWERS = {"study_question": "Does refusal stay under 5 percent?", "baseline": "parent at 0.70"}


def proj(run: Path) -> Path:
    """The project file for a run: it sits next to the run directory."""
    return Path(run).parent / "project.yaml"


def write_project(run: Path, answers: dict | None = None) -> Path:
    path = proj(run)
    path.write_text(yaml.safe_dump(answers or ANSWERS), encoding="utf-8")
    return path


def freeze(ledger: Ledger, run: Path) -> None:
    """Write the project file, then freeze it as the first ledger entry of a registered run."""
    write_project(run)
    freeze_run(ledger, proj(run))
