import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
HOOKS = ROOT / "hooks"


def hook(script: str, event: dict[str, Any], **env: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        [sys.executable, str(HOOKS / script)],
        input=json.dumps(event),
        capture_output=True,
        text=True,
        env={**os.environ, **env},
        check=False,
    )


def edit(tmp_path: Path, agent: str | None = "agent-harness:implementer") -> dict[str, Any]:
    event = {
        "hook_event_name": "PostToolUse",
        "tool_name": "Edit",
        "tool_input": {"file_path": str(tmp_path / "src" / "shop.py")},
        "cwd": str(tmp_path),
    }
    if agent:
        event["agent_type"] = agent
    return event


def test_hooks_json_points_at_existing_scripts():
    config = json.loads((HOOKS / "hooks.json").read_text())
    commands = [
        handler["command"]
        for groups in config["hooks"].values()
        for group in groups
        for handler in group["hooks"]
    ]
    for command in commands:
        script = command.split("/hooks/")[1].rstrip('"')
        assert (HOOKS / script).is_file()


def test_tests_run_only_for_the_implementer(tmp_path):
    failing = f"{sys.executable} -c 'import sys; sys.exit(1)'"
    for agent in (None, "agent-harness:reviewer-qa", "Explore"):
        result = hook("run_tests.py", edit(tmp_path, agent), HARNESS_TEST_COMMAND=failing)
        assert result.returncode == 0


def test_no_test_command_means_no_tests(tmp_path):
    result = hook("run_tests.py", edit(tmp_path), HARNESS_TEST_COMMAND="")
    assert result.returncode == 0


@pytest.mark.parametrize("agent", ["implementer", "agent-harness:implementer"])
def test_passing_tests_stay_quiet(tmp_path, agent):
    command = f"{sys.executable} -c 'print(\"ok\")'"
    result = hook("run_tests.py", edit(tmp_path, agent), HARNESS_TEST_COMMAND=command)
    assert result.returncode == 0
    assert result.stderr == ""


def test_failing_tests_go_back_to_the_implementer(tmp_path):
    script = "import sys; print('\\n'.join(f'line {i}' for i in range(100))); sys.exit(3)"
    command = f'{sys.executable} -c "{script}" {{file}}'

    result = hook(
        "run_tests.py", edit(tmp_path), HARNESS_TEST_COMMAND=command, CLAUDE_PROJECT_DIR=""
    )

    assert result.returncode == 2
    assert "Tests failed after editing src/shop.py" in result.stderr
    assert "src/shop.py" in result.stderr.splitlines()[0]
    assert "line 99" in result.stderr
    assert "line 30" not in result.stderr


def test_slow_tests_time_out(tmp_path):
    command = f"{sys.executable} -c 'import time; time.sleep(5)'"
    result = hook(
        "run_tests.py", edit(tmp_path), HARNESS_TEST_COMMAND=command, HARNESS_TEST_TIMEOUT="0.5"
    )
    assert result.returncode == 2
    assert "Tests did not finish within" in result.stderr


def guard(tool: str, agent: str | None = "agent-harness:jira-analyst") -> dict[str, Any] | None:
    event: dict[str, Any] = {"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": {}}
    if agent:
        event["agent_type"] = agent
    result = hook("guard_tracker_writes.py", event)
    assert result.returncode == 0
    return json.loads(result.stdout) if result.stdout else None


@pytest.mark.parametrize(
    "tool",
    [
        "mcp__plugin_jira-mcp_jira__add_comment",
        "mcp__plugin_jira-mcp_jira__update_description",
        "mcp__atlassian__transition_issue",
        "mcp__tracker__jira_create_task",
        "mcp__github__merge_pull_request",
    ],
)
def test_subagents_cannot_write_to_trackers(tool):
    verdict = guard(tool)
    assert verdict is not None
    assert verdict["hookSpecificOutput"]["permissionDecision"] == "deny"


@pytest.mark.parametrize(
    "tool",
    [
        "mcp__plugin_jira-mcp_jira__get_comments",
        "mcp__plugin_jira-mcp_jira__search_issues",
        "mcp__tracker__jira_get_worklogs",
        "mcp__ticket-precedent__effort_reference",
        "mcp__github__get_job_logs",
        "Edit",
    ],
)
def test_reads_and_other_tools_pass(tool):
    assert guard(tool) is None


def test_the_main_session_keeps_its_normal_permission_prompt():
    assert guard("mcp__plugin_jira-mcp_jira__add_comment", agent=None) is None
