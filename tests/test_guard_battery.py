"""The guardrail-tester battery (MIT, see tests/data/guard-battery.LICENSE) against the guard."""

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from tests.test_guard_shell import ENV, ROOT, SUBAGENT, guard_shell

DATA = Path(__file__).resolve().parent / "data"
BATTERY: list[dict[str, Any]] = [
    json.loads(line) for line in (DATA / "guard-battery.jsonl").read_text().splitlines() if line
]
POLICY: dict[str, dict[str, str]] = json.loads((DATA / "guard-battery-policy.json").read_text())
TOOLS = {"shell": "Bash", "write": "Write", "edit": "Edit", "read": "Read"}
VERDICTS = {"deny": "Denied", "allow": "Allowed by design", "gap": "Gap"}


def denial(tool: str, tool_input: dict[str, Any]) -> str | None:
    event = {
        "hook_event_name": "PreToolUse",
        "tool_name": TOOLS[tool],
        "tool_input": tool_input,
        "cwd": "/work/shop",
        "agent_type": SUBAGENT,
    }
    verdict = guard_shell.decision(event, ENV)
    if verdict is None:
        return None
    reason: str = verdict["hookSpecificOutput"]["permissionDecisionReason"]
    return reason


def cases() -> list[Any]:
    params = []
    for case in BATTERY:
        entry = POLICY.get(case["id"], {})
        gap = entry.get("verdict") == "gap"
        marks = [pytest.mark.xfail(strict=True, reason=entry["why"])] if gap else []
        params.append(pytest.param(case, id=case["id"], marks=marks))
    return params


def test_every_case_has_a_policy_with_a_reason():
    assert [case["id"] for case in BATTERY] == list(POLICY)
    assert len(BATTERY) == 90
    for entry in POLICY.values():
        assert entry["verdict"] in VERDICTS
        assert entry["why"].strip()


@pytest.mark.parametrize("case", cases())
def test_the_guard_meets_the_policy(case):
    entry = POLICY[case["id"]]
    reason = denial(case["tool"], case["input"])
    if entry["verdict"] == "allow":
        command = case["input"]["command"]
        assert "/build" in command
        assert reason is None
        assert denial("shell", {"command": command.replace("/build", "/src")}) is not None
        return
    assert reason is not None
    match = re.match(r"Rule ([\w-]+)", entry["why"])
    if match:
        assert reason.startswith(f"agent-harness guard, rule {match.group(1)}: "), reason


def coverage_table() -> str:
    counts: dict[str, Counter[str]] = {}
    for case in BATTERY:
        counts.setdefault(case["category"], Counter())[POLICY[case["id"]]["verdict"]] += 1
    counts["total"] = sum(counts.values(), Counter())
    rows = ["| Category | Denied | Allowed by design | Gap |", "|---|---|---|---|"]
    for category, count in counts.items():
        total = sum(count.values())
        rows.append(
            f"| {category} | {count['deny']} / {total} | {count['allow']} | {count['gap']} |"
        )
    return "\n".join(rows)


def test_the_readme_publishes_the_coverage_and_names_every_gap():
    readme = (ROOT / "README.md").read_text()
    assert coverage_table() in readme, coverage_table()
    gaps = [case_id for case_id, entry in POLICY.items() if entry["verdict"] == "gap"]
    assert [case_id for case_id in gaps if f"`{case_id}`" not in readme] == []
