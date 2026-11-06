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


def memory_guard(
    tool: str, path: str, agent: str | None = "agent-harness:estimator", cwd: str = "/repo"
) -> dict[str, Any] | None:
    event: dict[str, Any] = {
        "hook_event_name": "PreToolUse",
        "tool_name": tool,
        "tool_input": {"file_path": path},
        "cwd": cwd,
    }
    if agent:
        event["agent_type"] = agent
    result = hook("guard_memory_writes.py", event)
    assert result.returncode == 0
    return json.loads(result.stdout) if result.stdout else None


@pytest.mark.parametrize(
    ("agent", "path"),
    [
        (
            "agent-harness:estimator",
            "/home/u/.claude/agent-memory/agent-harness-estimator/MEMORY.md",
        ),
        (
            "agent-harness:jira-analyst",
            "/repo/.claude/agent-memory/agent-harness-jira-analyst/a.md",
        ),
        ("estimator", "/repo/.claude/agent-memory-local/estimator/MEMORY.md"),
        (
            "agent-harness:reviewer-qa",
            "/home/u/.claude/agent-memory/agent-harness-reviewer-qa/m.md",
        ),
    ],
)
def test_read_only_agents_write_to_their_own_memory(agent, path):
    assert memory_guard("Write", path, agent) is None


@pytest.mark.parametrize(
    "path",
    [
        "/repo/src/shop.py",
        "src/shop.py",
        "/home/u/.claude/agent-memory/agent-harness-jira-analyst/MEMORY.md",
        "/home/u/.claude/agent-memory/agent-harness-estimator",
        "/home/u/.claude/agent-memory/agent-harness-estimator/../../../CLAUDE.md",
        "",
    ],
)
@pytest.mark.parametrize("tool", ["Write", "Edit", "MultiEdit", "NotebookEdit"])
@pytest.mark.parametrize("agent", ["agent-harness:estimator", "agent-harness:reviewer-qa"])
def test_read_only_agents_cannot_write_elsewhere(tool, path, agent):
    verdict = memory_guard(tool, path, agent)
    assert verdict is not None
    assert verdict["hookSpecificOutput"]["permissionDecision"] == "deny"


@pytest.mark.parametrize("agent", [None, "agent-harness:implementer", "general-purpose"])
def test_other_agents_and_the_main_session_are_not_limited(agent):
    assert memory_guard("Write", "/repo/src/shop.py", agent) is None


def test_reading_stays_allowed():
    assert memory_guard("Read", "/repo/src/shop.py") is None


def transcript(tmp_path: Path) -> Path:
    def entry(kind: str, stamp: str, **fields: Any) -> str:
        return json.dumps({"type": kind, "timestamp": stamp, **fields})

    def assistant(stamp: str, request: str, tools: int, output: int) -> str:
        content = [{"type": "tool_use", "name": "Bash"}] * tools + [{"type": "text", "text": "ok"}]
        usage = {"input_tokens": 2, "output_tokens": output, "cache_read_input_tokens": 100}
        message = {"model": "claude-sonnet", "content": content, "usage": usage}
        return entry("assistant", stamp, requestId=request, message=message)

    path = tmp_path / "subagents" / "agent-a1.jsonl"
    path.parent.mkdir()
    lines = [
        entry("user", "2026-01-01T10:00:00Z", message={"content": "Estimate SHOP-412 please"}),
        assistant("2026-01-01T10:00:30Z", "r1", tools=2, output=50),
        assistant("2026-01-01T10:00:31Z", "r1", tools=1, output=50),
        entry("user", "2026-01-01T10:01:00Z", message={"content": "about OTHER-1"}),
        assistant("2026-01-01T10:02:00Z", "r2", tools=0, output=20),
    ]
    path.write_text("\n".join([*lines, "not json"]) + "\n")
    meta = {"agentType": "agent-harness:estimator", "description": "Estimate"}
    path.with_suffix(".meta.json").write_text(json.dumps(meta))
    return path


def log_run(tmp_path: Path, event: dict[str, Any]) -> list[dict[str, Any]]:
    log = tmp_path / "runs.jsonl"
    result = hook("log_agent_runs.py", event, HARNESS_RUNS_LOG=str(log))
    assert result.returncode == 0
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def test_a_finished_subagent_run_is_logged(tmp_path):
    event = {
        "hook_event_name": "SubagentStop",
        "session_id": "s1",
        "cwd": "/work/shop",
        "agent_id": "a1",
        "agent_transcript_path": str(transcript(tmp_path)),
    }

    [run] = log_run(tmp_path, event)

    assert run["agent_type"] == "agent-harness:estimator"
    assert run["project"] == "shop"
    assert run["ticket"] == "SHOP-412"
    assert run["seconds"] == 120.0
    assert run["requests"] == 2
    assert run["tool_uses"] == 3
    assert run["tokens"]["output_tokens"] == 70
    assert run["tokens"]["cache_read_input_tokens"] == 200
    assert run["model"] == "claude-sonnet"


def test_runs_are_appended(tmp_path):
    event = {"agent_id": "a1", "agent_transcript_path": str(transcript(tmp_path))}
    log_run(tmp_path, event)
    assert len(log_run(tmp_path, event)) == 2


@pytest.mark.parametrize("event", [{}, {"agent_transcript_path": "/nowhere/agent.jsonl"}])
def test_a_missing_transcript_logs_nothing(tmp_path, event):
    assert log_run(tmp_path, event) == []


def test_bad_input_never_blocks_the_session(tmp_path):
    result = subprocess.run(  # noqa: S603
        [sys.executable, str(HOOKS / "log_agent_runs.py")],
        input="not json",
        capture_output=True,
        text=True,
        env={**os.environ, "HARNESS_RUNS_LOG": str(tmp_path / "runs.jsonl")},
        check=False,
    )
    assert result.returncode == 0
