"""Execution census: proof that a gate examined what it claims to have examined.

A gate that returns ``passed: true`` after examining nothing looks the same as one that examined everything and
found nothing wrong. A census tells them apart. Build a fresh ``Census`` inside the gate's check on every call (a
shared one would double-count when ``promote`` re-runs a cheap gate), and return it in the result:

    def check(run_dir):
        census = Census("split_integrity", REQUIRED_NONEMPTY)
        for arm in arms(run_dir):
            census.discover(arm)
            if ok(arm):
                census.passed_item(arm)
            else:
                census.failed_item(arm, reason="hash mismatch")
        return {"passed": census.failed == 0, "census": census.to_dict()}

Rules, checked by ``validate`` on the object and by ``validate_census_dict`` on a stored receipt:

* every discovered item has exactly one outcome (passed, failed, skipped or blocked), and nothing has an outcome
  without being discovered, so ``discovered == checked + skipped + blocked`` and ``checked == passed + failed``;
* every failure, skip and block has a reason;
* ``REQUIRED_NONEMPTY`` refuses an empty population; ``CONDITIONALLY_REQUIRED`` does too when its precondition holds.

``run_gate`` refuses a result whose census breaks a rule, or that says ``passed: true`` while the census has failures,
and writes no receipt. ``promote`` applies the same rules to stored receipts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

REQUIRED_NONEMPTY = "REQUIRED_NONEMPTY"
CONDITIONALLY_REQUIRED = "CONDITIONALLY_REQUIRED"
OPTIONAL = "OPTIONAL"
POLICIES = (REQUIRED_NONEMPTY, CONDITIONALLY_REQUIRED, OPTIONAL)


class CensusError(ValueError):
    """A census rule is broken. This is a gate failure, not a data failure."""


@dataclass
class Census:
    gate: str
    policy: str = REQUIRED_NONEMPTY
    precondition_met: bool = True
    _discovered: list[str] = field(default_factory=list, repr=False)
    _outcomes: dict[str, tuple[str, str | None]] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if self.policy not in POLICIES:
            raise CensusError(f"policy must be one of {POLICIES}")

    def discover(self, item: str) -> None:
        if item in self._discovered:
            raise CensusError(f"{item!r} was discovered twice")
        self._discovered.append(item)

    def _record(self, item: str, outcome: str, reason: str | None) -> None:
        if item not in self._discovered:
            raise CensusError(f"{item!r} has an outcome but was never discovered")
        if item in self._outcomes:
            raise CensusError(f"{item!r} already has the outcome {self._outcomes[item][0]!r}")
        if outcome != "passed" and not (reason and reason.strip()):
            raise CensusError(f"{item!r} is {outcome} without a reason")
        self._outcomes[item] = (outcome, reason)

    def passed_item(self, item: str) -> None:
        self._record(item, "passed", None)

    def failed_item(self, item: str, reason: str) -> None:
        self._record(item, "failed", reason)

    def skipped_item(self, item: str, reason: str) -> None:
        self._record(item, "skipped", reason)

    def blocked_item(self, item: str, reason: str) -> None:
        self._record(item, "blocked", reason)

    def _count(self, outcome: str) -> int:
        return sum(1 for o, _ in self._outcomes.values() if o == outcome)

    @property
    def failed(self) -> int:
        return self._count("failed")

    def validate(self) -> None:
        missing = [item for item in self._discovered if item not in self._outcomes]
        if missing:
            raise CensusError(f"census for {self.gate!r}: discovered items without an outcome: {missing}")
        validate_census_dict(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        def listed(outcome: str) -> list[dict[str, Any]]:
            return [{"item": i, "reason": r} for i, (o, r) in self._outcomes.items() if o == outcome]

        return {
            "gate": self.gate,
            "policy": self.policy,
            "precondition_met": self.precondition_met,
            "discovered": len(self._discovered),
            "checked": self._count("passed") + self._count("failed"),
            "passed": self._count("passed"),
            "failed": self._count("failed"),
            "skipped": self._count("skipped"),
            "blocked": self._count("blocked"),
            "failures": listed("failed"),
            "skips": listed("skipped"),
            "blocks": listed("blocked"),
        }


def validate_census_dict(census: Any) -> None:
    """Raise CensusError unless ``census`` is a well-formed census dict whose counts add up."""
    if not isinstance(census, dict):
        raise CensusError("census must be a JSON object")
    keys = ("discovered", "checked", "passed", "failed", "skipped", "blocked")
    for key in keys:
        value = census.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise CensusError(f"census {key} must be a non-negative integer, got {value!r}")
    errors = []
    if census["discovered"] != census["checked"] + census["skipped"] + census["blocked"]:
        errors.append(f"discovered ({census['discovered']}) != checked ({census['checked']}) + skipped "
                      f"({census['skipped']}) + blocked ({census['blocked']})")
    if census["checked"] != census["passed"] + census["failed"]:
        errors.append(f"checked ({census['checked']}) != passed ({census['passed']}) + failed ({census['failed']})")
    for listing, count in (("failures", "failed"), ("skips", "skipped"), ("blocks", "blocked")):
        items = census.get(listing, [])
        if not isinstance(items, list) or len(items) != census[count]:
            errors.append(f"{listing} lists {len(items) if isinstance(items, list) else '?'} items but {count} is "
                          f"{census[count]}")
        elif any(not isinstance(i, dict) or not str(i.get("reason") or "").strip() for i in items):
            errors.append(f"every entry in {listing} needs a reason")
    policy = census.get("policy")
    if policy not in POLICIES:
        errors.append(f"policy must be one of {POLICIES}")
    elif census["discovered"] == 0 and (
        policy == REQUIRED_NONEMPTY or (policy == CONDITIONALLY_REQUIRED and census.get("precondition_met", True))
    ):
        errors.append(f"the population is required but empty (policy {policy})")
    if errors:
        raise CensusError(f"census for {census.get('gate')!r}: " + "; ".join(errors))


def check_result_census(result: Any) -> None:
    """Raise CensusError if the result carries a census that is broken or contradicts ``passed``."""
    if not isinstance(result, dict) or "census" not in result:
        return
    validate_census_dict(result["census"])
    if result.get("passed") is True and result["census"]["failed"] > 0:
        raise CensusError(f"the result says passed but its census has {result['census']['failed']} failure(s)")
