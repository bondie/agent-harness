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


def edit(
    tmp_path: Path, agent: str | None = "agent-harness:implementer", file: Path | None = None
) -> dict[str, Any]:
    event = {
        "hook_event_name": "PostToolUse",
        "tool_name": "Edit",
        "tool_input": {"file_path": str(file or tmp_path / "src" / "shop.py")},
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


def git_repository(path: Path) -> Path:
    (path / "src").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)  # noqa: S603, S607
    return path.resolve()


def write_settings(repository: Path, name: str, **env: str | None) -> None:
    (repository / ".claude").mkdir(exist_ok=True)
    (repository / ".claude" / name).write_text(json.dumps({"env": env}))


def reporting(label: str) -> str:
    script = f"import os, sys; print('{label}', os.getcwd(), *sys.argv[1:]); sys.exit(1)"
    return f'{sys.executable} -c "{script}" {{file}}'


@pytest.mark.parametrize("local", [False, True])
def test_a_file_in_another_repository_runs_that_repositorys_tests(tmp_path, local):
    project = git_repository(tmp_path / "project")
    platform = git_repository(tmp_path / "platform")
    write_settings(platform, "settings.json", HARNESS_TEST_COMMAND=reporting("shared"))
    if local:
        write_settings(platform, "settings.local.json", HARNESS_TEST_COMMAND=reporting("local"))
    event = edit(project, file=platform / "src" / "feed.py")

    result = hook(
        "run_tests.py",
        event,
        HARNESS_TEST_COMMAND=reporting("session"),
        CLAUDE_PROJECT_DIR=str(project),
    )

    assert result.returncode == 2
    assert result.stderr.startswith("Tests failed after editing src/feed.py")
    label = "local" if local else "shared"
    assert result.stderr.splitlines()[-1] == f"{label} {platform} src/feed.py"


def test_another_repository_sets_its_own_timeout(tmp_path):
    project = git_repository(tmp_path / "project")
    platform = git_repository(tmp_path / "platform")
    slow = f"{sys.executable} -c 'import time; time.sleep(5)'"
    write_settings(platform, "settings.json", HARNESS_TEST_COMMAND=slow)
    write_settings(platform, "settings.local.json", HARNESS_TEST_TIMEOUT="0.5")
    event = edit(project, file=platform / "src" / "feed.py")

    result = hook(
        "run_tests.py",
        event,
        HARNESS_TEST_COMMAND=reporting("session"),
        HARNESS_TEST_TIMEOUT="300",
        CLAUDE_PROJECT_DIR=str(project),
    )

    assert result.returncode == 2
    assert "Tests did not finish within" in result.stderr


@pytest.mark.parametrize("null", [False, True])
def test_another_repository_without_a_test_command_runs_no_tests(tmp_path, null):
    project = git_repository(tmp_path / "project")
    platform = git_repository(tmp_path / "platform")
    if null:
        write_settings(platform, "settings.json", HARNESS_TEST_COMMAND=None)
    event = edit(project, file=platform / "src" / "feed.py")

    result = hook(
        "run_tests.py",
        event,
        HARNESS_TEST_COMMAND=reporting("session"),
        CLAUDE_PROJECT_DIR=str(project),
    )

    assert result.returncode == 0
    assert result.stderr == ""
    output = json.loads(result.stdout)["hookSpecificOutput"]
    assert output["hookEventName"] == "PostToolUse"
    notice = output["additionalContext"]
    assert notice.startswith(
        f"No tests ran after editing src/feed.py because {platform} sets no HARNESS_TEST_COMMAND"
    )
    assert "so the team lead can decide whether to run them" in notice
    assert "yourself" not in notice


def test_no_test_command_anywhere_stays_silent(tmp_path):
    project = git_repository(tmp_path / "project")
    platform = git_repository(tmp_path / "platform")
    event = edit(project, file=platform / "src" / "feed.py")

    result = hook("run_tests.py", event, HARNESS_TEST_COMMAND="", CLAUDE_PROJECT_DIR=str(project))

    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""


@pytest.mark.parametrize(
    "broken",
    ["{not json", json.dumps({"env": ["HARNESS_TEST_COMMAND"]}), json.dumps(["env"])],
)
def test_unusable_settings_in_another_repository_are_skipped(tmp_path, broken):
    project = git_repository(tmp_path / "project")
    platform = git_repository(tmp_path / "platform")
    write_settings(platform, "settings.json", HARNESS_TEST_COMMAND=reporting("shared"))
    (platform / ".claude" / "settings.local.json").write_text(broken)
    event = edit(project, file=platform / "src" / "feed.py")

    result = hook(
        "run_tests.py",
        event,
        HARNESS_TEST_COMMAND=reporting("session"),
        CLAUDE_PROJECT_DIR=str(project),
    )

    assert result.returncode == 2
    assert result.stderr.splitlines()[-1] == f"shared {platform} src/feed.py"


def test_a_null_setting_in_another_repository_is_ignored(tmp_path):
    project = git_repository(tmp_path / "project")
    platform = git_repository(tmp_path / "platform")
    write_settings(platform, "settings.json", HARNESS_TEST_COMMAND=reporting("shared"))
    write_settings(platform, "settings.local.json", HARNESS_TEST_COMMAND=None)
    event = edit(project, file=platform / "src" / "feed.py")

    result = hook(
        "run_tests.py",
        event,
        HARNESS_TEST_COMMAND=reporting("session"),
        CLAUDE_PROJECT_DIR=str(project),
    )

    assert result.returncode == 2
    assert result.stderr.splitlines()[-1] == f"shared {platform} src/feed.py"


@pytest.mark.parametrize("timeout", ["abc", "nan", "inf", "-1", "0"])
@pytest.mark.parametrize("repository", ["project", "platform"])
def test_an_invalid_timeout_falls_back_to_the_default(tmp_path, repository, timeout):
    project = git_repository(tmp_path / "project")
    platform = git_repository(tmp_path / "platform")
    write_settings(
        platform,
        "settings.json",
        HARNESS_TEST_COMMAND=reporting("platform"),
        HARNESS_TEST_TIMEOUT=timeout,
    )
    event = edit(project, file=tmp_path / repository / "src" / "feed.py")

    result = hook(
        "run_tests.py",
        event,
        HARNESS_TEST_COMMAND=reporting("session"),
        HARNESS_TEST_TIMEOUT=timeout,
        CLAUDE_PROJECT_DIR=str(project),
    )

    assert result.returncode == 2
    assert result.stderr.startswith("Tests failed after editing src/feed.py")


def test_without_git_an_edit_runs_the_session_tests(tmp_path):
    project = git_repository(tmp_path / "project")
    platform = git_repository(tmp_path / "platform")
    write_settings(platform, "settings.json", HARNESS_TEST_COMMAND=reporting("platform"))
    (tmp_path / "empty").mkdir()
    event = edit(project, file=platform / "src" / "feed.py")

    result = hook(
        "run_tests.py",
        event,
        HARNESS_TEST_COMMAND=reporting("session"),
        CLAUDE_PROJECT_DIR=str(project),
        PATH=str(tmp_path / "empty"),
    )

    assert result.returncode == 2
    assert result.stderr.splitlines()[-1] == f"session {project} ../platform/src/feed.py"


def test_a_repository_nested_in_the_session_runs_the_session_tests(tmp_path):
    project = git_repository(tmp_path / "project")
    nested = git_repository(project / "vendor" / "platform")
    write_settings(nested, "settings.json", HARNESS_TEST_COMMAND=reporting("nested"))
    event = edit(project, file=nested / "src" / "feed.py")

    result = hook(
        "run_tests.py",
        event,
        HARNESS_TEST_COMMAND=reporting("session"),
        CLAUDE_PROJECT_DIR=str(project),
    )

    assert result.returncode == 2
    assert result.stderr.splitlines()[-1] == f"session {project} vendor/platform/src/feed.py"


@pytest.mark.parametrize("linked", ["session", "file"])
def test_a_symlinked_session_directory_keeps_the_file_relative(tmp_path, linked):
    project = git_repository(tmp_path / "project")
    link = tmp_path / "link"
    link.symlink_to(project, target_is_directory=True)
    session, edited = (link, project) if linked == "session" else (project, link)
    event = edit(session, file=edited / "src" / "shop.py")

    result = hook(
        "run_tests.py",
        event,
        HARNESS_TEST_COMMAND=reporting("session"),
        CLAUDE_PROJECT_DIR=str(session),
    )

    assert result.returncode == 2
    assert result.stderr.splitlines()[-1] == f"session {project} src/shop.py"


@pytest.mark.parametrize("subdirectory", ["", "server"])
def test_a_file_in_the_session_repository_runs_the_session_tests(tmp_path, subdirectory):
    session = git_repository(tmp_path / "project") / subdirectory
    (session / "src").mkdir(parents=True, exist_ok=True)
    write_settings(session, "settings.json", HARNESS_TEST_COMMAND=reporting("repository"))
    event = edit(session, file=session / "src" / "shop.py")

    result = hook(
        "run_tests.py",
        event,
        HARNESS_TEST_COMMAND=reporting("session"),
        CLAUDE_PROJECT_DIR=str(session),
    )

    assert result.returncode == 2
    assert result.stderr.splitlines()[-1] == f"session {session} src/shop.py"


def test_a_file_outside_git_runs_the_session_tests(tmp_path):
    project = git_repository(tmp_path / "project")
    loose = tmp_path / "loose"
    loose.mkdir()
    event = edit(project, file=loose / "notes.py")

    result = hook(
        "run_tests.py",
        event,
        HARNESS_TEST_COMMAND=reporting("session"),
        CLAUDE_PROJECT_DIR=str(project),
        GIT_CEILING_DIRECTORIES=str(tmp_path),
    )

    assert result.returncode == 2
    assert result.stderr.splitlines()[-1] == f"session {project} ../loose/notes.py"


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
    tool: str,
    path: str,
    agent: str | None = "agent-harness:estimator",
    cwd: str = "/repo",
    home: Path = HOOKS / "no-harness-home",
    **tool_input: Any,
) -> dict[str, Any] | None:
    event: dict[str, Any] = {
        "hook_event_name": "PreToolUse",
        "tool_name": tool,
        "tool_input": {"file_path": path, **tool_input},
        "cwd": cwd,
    }
    if agent:
        event["agent_type"] = agent
    result = hook("guard_memory_writes.py", event, HARNESS_HOME=str(home))
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


VALID = "- Run the specs with make spec (source: SHOP-412, 2026-01-15)"
LEGACY = "- An old note without a date"
FORMAT = "`- <lesson text> (source: <KEY|MR|commit|retro>, YYYY-MM-DD[, expires YYYY-MM-DD])`"
NO_TRAILER = "it does not end with (source: …, YYYY-MM-DD)"


def memory_file(
    tmp_path: Path, name: str = "MEMORY.md", content: str | None = None, agent: str = "implementer"
) -> Path:
    path = tmp_path / ".claude" / "agent-memory" / f"agent-harness-{agent}" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    if content is not None:
        path.write_text(content)
    return path


def write(path: Path, content: str, **options: Any) -> dict[str, Any] | None:
    return memory_guard("Write", str(path), "agent-harness:implementer", content=content, **options)


def change(path: Path, old: str, new: str, **options: Any) -> dict[str, Any] | None:
    return memory_guard(
        "Edit", str(path), "agent-harness:implementer", old_string=old, new_string=new, **options
    )


def denial(verdict: dict[str, Any] | None) -> str:
    assert verdict is not None
    assert verdict["hookSpecificOutput"]["permissionDecision"] == "deny"
    return str(verdict["hookSpecificOutput"]["permissionDecisionReason"])


def config(tmp_path: Path, text: str) -> Path:
    home = tmp_path / "harness"
    home.mkdir(exist_ok=True)
    (home / "memory.json").write_text(text)
    return home


def lines_of(kb: float) -> str:
    line = VALID + "\n"
    return line * (int(kb * 1024) // len(line) + 1)


@pytest.mark.parametrize("agent", ["implementer", "jira-analyst", "estimator", "reviewer-qa"])
def test_every_agent_writes_lessons_in_the_format(tmp_path, agent):
    path = memory_file(tmp_path, agent=agent)
    verdict = memory_guard("Write", str(path), f"agent-harness:{agent}", content=f"{VALID}\n")
    assert verdict is None


@pytest.mark.parametrize("agent", ["implementer", "jira-analyst", "estimator", "reviewer-qa"])
def test_every_agent_is_held_to_the_format(tmp_path, agent):
    path = memory_file(tmp_path, agent=agent)
    verdict = memory_guard("Write", str(path), f"agent-harness:{agent}", content=f"{LEGACY}\n")
    assert f"line 1: {NO_TRAILER}" in denial(verdict)


@pytest.mark.parametrize(
    "lesson",
    [
        VALID,
        "- Gone with the next release (source: !786, 2026-01-15, expires 2026-03-01)",
        "- Learned from a commit (source: 5b4636d, 2026-01-15)",
        "- Learned in a retro (source: retro, 2026-01-15)",
        "- A lesson that wraps\n  onto a second line (source: SHOP-412, 2026-01-15)",
        "- A headline (source: SHOP-412, 2026-01-15)\n  - with a detail below",
        "* A star bullet (source: SHOP-412, 2026-01-15)",
    ],
)
def test_lessons_in_the_format_pass(tmp_path, lesson):
    assert write(memory_file(tmp_path, "topic.md"), f"# Notes\n\n{lesson}\n") is None


@pytest.mark.parametrize(
    ("lesson", "problem"),
    [
        (LEGACY, NO_TRAILER),
        ("- Ends in prose (source: SHOP-412, 2026-01-15) and more", "it does not end with"),
        ("+ A plus bullet", "it does not end with"),
        ("- A date alone (2026-01-15)", "it has no source"),
        ("- An empty source (source: , 2026-01-15)", "it has no source"),
        ("- A month 13 (source: SHOP-412, 2026-13-01)", "2026-13-01 is not a date"),
        (
            "- Expires first (source: SHOP-412, 2026-01-15, expires 2026-01-01)",
            "expires 2026-01-01 is not a date after 2026-01-15",
        ),
        (
            "- Expires on a day 32 (source: SHOP-412, 2026-01-15, expires 2026-01-32)",
            "expires 2026-01-32 is not a date after 2026-01-15",
        ),
    ],
)
def test_lessons_without_source_and_date_are_denied(tmp_path, lesson, problem):
    reason = denial(write(memory_file(tmp_path, "topic.md"), f"# Notes\n\n{lesson}\n"))
    assert f"- line 3: {problem}" in reason
    assert f"`{lesson}`" in reason
    assert FORMAT in reason


def test_headings_front_matter_prose_and_code_are_not_lessons(tmp_path):
    content = "\n".join(
        [
            "---",
            "name: notes",
            "- a front matter list item",
            "---",
            "# Notes",
            "Plain prose is not a lesson.",
            "  An indented line after prose neither.",
            "```yaml",
            "- not: a lesson",
            "```",
            VALID,
        ]
    )
    assert write(memory_file(tmp_path, "topic.md"), content) is None


def test_index_links_in_memory_md_need_no_source(tmp_path):
    link = "- [Commands](commands.md) — how to run the tests"
    assert write(memory_file(tmp_path), f"# Index\n\n{link}\n") is None
    assert "line 1: it does not end with" in denial(write(memory_file(tmp_path, "t.md"), link))
    url = "- [Docs](https://example.com/docs.md) — the manual"
    assert "line 1: it does not end with" in denial(write(memory_file(tmp_path), url))


def test_lines_already_in_the_file_are_not_checked(tmp_path):
    path = memory_file(tmp_path, "topic.md", f"{LEGACY}\n- Another old note\n")

    assert write(path, f"- Another old note\n{LEGACY}\n{VALID}\n") is None
    assert change(path, f"{LEGACY}\n", f"{LEGACY}\n{VALID}\n") is None
    assert change(path, "- Another old note\n", "") is None


def test_a_changed_line_needs_the_format(tmp_path):
    path = memory_file(tmp_path, "topic.md", f"# Notes\n{LEGACY}\n")
    changed = f"{LEGACY}, changed"

    assert f"line 2: {NO_TRAILER}: `{changed}`" in denial(write(path, f"# Notes\n{changed}\n"))
    assert f"line 1 of new_string: {NO_TRAILER}: `{changed}`" in (
        denial(change(path, LEGACY, changed))
    )
    assert change(path, LEGACY, f"{LEGACY} (source: SHOP-412, 2026-01-15)") is None


def test_an_edit_checks_only_the_lines_its_new_string_adds(tmp_path):
    path = memory_file(tmp_path, "topic.md", f"{LEGACY}\n- Another old note\n")

    reason = denial(change(path, "- Another old note", "- Another old note\n- A new note"))

    assert f"- line 2 of new_string: {NO_TRAILER}: `- A new note`" in reason
    assert LEGACY not in reason
    assert "Another old note" not in reason


def test_a_multi_edit_checks_every_edit(tmp_path):
    path = memory_file(tmp_path, "topic.md", f"{LEGACY}\n")
    edits = [
        {"old_string": LEGACY, "new_string": f"{LEGACY}\n{VALID}"},
        {"old_string": VALID, "new_string": f"{VALID}\n- Undated"},
    ]

    agent = "agent-harness:implementer"

    reason = denial(memory_guard("MultiEdit", str(path), agent, edits=edits))

    assert f"line 2 of new_string: {NO_TRAILER}: `- Undated`" in reason
    assert memory_guard("MultiEdit", str(path), agent, edits=edits[:1]) is None
    assert memory_guard("MultiEdit", str(path), agent, edits="x") is None


def test_the_reason_names_at_most_five_lines(tmp_path):
    content = "\n".join(f"- Note {number}" for number in range(8))

    reason = denial(write(memory_file(tmp_path, "topic.md"), content))

    assert "`- Note 4`" in reason
    assert "`- Note 5`" not in reason
    assert "- and 3 more" in reason


def test_the_source_can_be_made_optional(tmp_path):
    home = config(tmp_path, json.dumps({"require_source": False}))
    path = memory_file(tmp_path, "topic.md")

    assert write(path, "- A date alone (2026-01-15)\n", home=home) is None
    assert "it does not end with" in denial(write(path, f"{LEGACY}\n", home=home))


@pytest.mark.parametrize(("name", "cap"), [("MEMORY.md", 4), ("topic.md", 8)])
def test_a_file_over_its_cap_is_denied(tmp_path, name, cap):
    path = memory_file(tmp_path, name)
    assert write(path, lines_of(cap - 0.5)) is None

    reason = denial(write(path, lines_of(cap + 0.5)))

    assert f"over the {cap} KB cap" in reason
    assert "compact or move detail to a topic file" in reason


def test_a_write_that_shrinks_an_oversized_file_is_allowed(tmp_path):
    path = memory_file(tmp_path, content="# Index\n" + lines_of(10))

    assert write(path, lines_of(6)) is None
    assert change(path, "# Index\n", "") is None
    assert "over the 4 KB cap for the memory index" in denial(
        change(path, "# Index\n", f"# Index\n{VALID}\n")
    )


def test_an_edit_the_tool_cannot_apply_is_left_to_the_tool(tmp_path):
    path = memory_file(tmp_path, content=lines_of(10))
    assert change(path, "- Not in the file", f"{VALID}\n") is None
    assert change(memory_file(tmp_path, "missing.md"), "", f"{VALID}\n") is None


def test_caps_come_from_memory_json(tmp_path):
    home = config(tmp_path, json.dumps({"max_file_kb": 1, "max_index_kb": 12.5}))

    assert write(memory_file(tmp_path), lines_of(12), home=home) is None
    assert "over the 1 KB cap for a memory file" in denial(
        write(memory_file(tmp_path, "topic.md"), lines_of(2), home=home)
    )


@pytest.mark.parametrize(
    "settings",
    [
        '{"max_index_kb": NaN, "require_source": "no"}',
        '{"max_index_kb": Infinity, "require_source": null}',
        '{"max_index_kb": -1}',
        '{"max_index_kb": 0}',
        '{"max_index_kb": "12"}',
        '{"max_index_kb": true}',
        '{"max_index_kb": null}',
        "[12]",
        "not json",
    ],
)
def test_unusable_settings_fall_back_to_the_defaults(tmp_path, settings):
    home = config(tmp_path, settings)
    path = memory_file(tmp_path)

    assert "over the 4 KB cap" in denial(write(path, lines_of(5), home=home))
    assert "it has no source" in denial(write(path, "- A date alone (2026-01-15)\n", home=home))


@pytest.mark.parametrize(
    ("agent", "target"),
    [
        (None, "agent-harness-implementer/MEMORY.md"),
        ("general-purpose", "agent-harness-implementer/MEMORY.md"),
        ("agent-harness:implementer", "agent-harness-estimator/MEMORY.md"),
    ],
)
def test_only_the_agents_own_memory_is_checked(tmp_path, agent, target):
    path = tmp_path / ".claude" / "agent-memory" / target
    assert memory_guard("Write", str(path), agent, content=f"{LEGACY}\n") is None


def test_the_implementer_writes_other_files_unchecked(tmp_path):
    assert write(tmp_path / "README.md", f"{LEGACY}\n") is None


def test_a_relative_path_into_memory_is_checked(tmp_path):
    path = memory_file(tmp_path, "topic.md")
    verdict = memory_guard(
        "Write", "topic.md", "agent-harness:implementer", cwd=str(path.parent), content=LEGACY
    )
    assert "it does not end with" in denial(verdict)


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


@pytest.mark.parametrize("git", ["dir", "file"])
def test_the_project_is_the_repository_not_the_subdirectory(tmp_path, git):
    repo = tmp_path / "shop"
    (repo / "src" / "server").mkdir(parents=True)
    if git == "dir":
        (repo / ".git").mkdir()
    else:
        (repo / ".git").write_text("gitdir: /elsewhere\n")
    event = {
        "cwd": str(repo / "src" / "server"),
        "agent_id": "a1",
        "agent_transcript_path": str(transcript(tmp_path)),
    }

    [run] = log_run(tmp_path, event)

    assert run["project"] == "shop"


def test_runs_are_appended(tmp_path):
    event = {"agent_id": "a1", "agent_transcript_path": str(transcript(tmp_path))}
    log_run(tmp_path, event)
    assert len(log_run(tmp_path, event)) == 2


@pytest.mark.parametrize("event", [{}, {"agent_transcript_path": "/nowhere/agent.jsonl"}])
def test_a_missing_transcript_logs_nothing(tmp_path, event):
    assert log_run(tmp_path, event) == []


@pytest.mark.parametrize("payload", ["not json", "[1, 2]"])
def test_bad_input_lets_the_memory_write_through_and_logs_it(tmp_path, payload):
    result = subprocess.run(  # noqa: S603
        [sys.executable, str(HOOKS / "guard_memory_writes.py")],
        input=payload,
        capture_output=True,
        text=True,
        env={**os.environ, "HARNESS_HOME": str(tmp_path)},
        check=False,
    )
    assert (result.returncode, result.stdout) == (0, "")
    assert "guard_memory_writes.py allowed the write after an internal error" in result.stderr
    assert "Traceback" not in result.stderr


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


ESTIMATE = {
    "ticket": "SHOP-412",
    "mode": "reference",
    "reference_median": 1.5,
    "parts": [],
    "adjustments": [{"hours": 0.5, "reason": "a migration on top", "citation": "SHOP-398"}],
    "total": 2.0,
}


def estimate_report(**changes: Any) -> str:
    block = json.dumps({**ESTIMATE, **changes})
    return f"- **Estimate:** 2 h\n- **Basis:** SHOP-398\n\n```json harness-estimate\n{block}\n```\n"


def estimator_stop(
    tmp_path: Path, report: str | None, agent: str = "agent-harness:estimator", **fields: Any
) -> dict[str, Any]:
    event: dict[str, Any] = {
        "hook_event_name": "SubagentStop",
        "session_id": "s1",
        "agent_id": "a1",
        "agent_type": agent,
        "stop_hook_active": False,
        "cwd": str(tmp_path),
        **fields,
    }
    if report is not None:
        event["last_assistant_message"] = report
    return event


def check_estimate(tmp_path: Path, event: dict[str, Any]) -> dict[str, Any] | None:
    result = hook("estimate_check.py", event, HARNESS_HOME=str(tmp_path / "home"))
    assert result.returncode == 0
    return json.loads(result.stdout) if result.stdout else None


def test_a_valid_estimate_stops_quietly(tmp_path):
    assert check_estimate(tmp_path, estimator_stop(tmp_path, estimate_report())) is None


@pytest.mark.parametrize(
    ("report", "expected"),
    [
        ("- **Estimate:** 2 h\n- **Basis:** SHOP-398", "no ```json harness-estimate block"),
        (
            estimate_report(adjustments=[{"hours": 1, "reason": "bigger", "citation": ""}]),
            "has no citation",
        ),
        (estimate_report(total=3), "Set total to 2"),
        ("- **Estimate:** 2 h\n```json harness-estimate\n{oops\n```", "not valid JSON"),
    ],
)
def test_an_invalid_estimate_goes_back_to_the_estimator(tmp_path, report, expected):
    verdict = check_estimate(tmp_path, estimator_stop(tmp_path, report))
    assert verdict is not None
    assert verdict["decision"] == "block"
    assert expected in verdict["reason"]
    assert "Reply with the whole report again" in verdict["reason"]


def test_the_estimator_is_sent_back_at_most_twice(tmp_path):
    report = estimate_report(total=3)
    verdicts = [
        check_estimate(tmp_path, estimator_stop(tmp_path, report, stop_hook_active=active))
        for active in (False, True, True, True)
    ]

    assert [v["decision"] for v in verdicts[:2] if v] == ["block", "block"]
    for last in verdicts[2:]:
        assert last is not None
        assert "decision" not in last
        assert "after 2 retries" in last["systemMessage"]
        assert "Set total to 2" in last["systemMessage"]
    record = json.loads((tmp_path / "home" / "estimate-check" / "s1-a1.json").read_text())
    assert record["blocks"] == 2

    resumed = check_estimate(tmp_path, estimator_stop(tmp_path, report))
    assert resumed is not None
    assert resumed["decision"] == "block"


@pytest.mark.parametrize("task", ["Retro for SHOP-412.", "SHOP-412: Retro for SHOP-412."])
def test_a_retro_run_of_the_estimator_is_not_checked(tmp_path, task):
    path = tmp_path / "subagents" / "agent-a1.jsonl"
    path.parent.mkdir()
    entries = [
        {"type": "user", "isMeta": True, "message": {"content": "Estimate SHOP-1"}},
        {"type": "user", "message": {"content": [{"type": "text", "text": task}]}},
    ]
    path.write_text("\n".join(map(json.dumps, entries)) + "\n")
    invalid = estimate_report(total=3)
    stop = estimator_stop(tmp_path, invalid, agent_transcript_path=str(path))
    handing = {**estimator_handback(invalid), "transcript_path": f"{tmp_path}.jsonl"}

    assert check_estimate(tmp_path, stop) is None
    assert check_estimate(tmp_path, handing) is None
    assert not (tmp_path / "home").exists()


def test_an_unwritable_home_lets_the_estimate_through_and_says_so(tmp_path):
    (tmp_path / "home").write_text("a file, not a directory")
    invalid = estimate_report(total=3)

    for event in (estimator_stop(tmp_path, invalid), estimator_handback(invalid)):
        verdict = check_estimate(tmp_path, event)
        assert verdict is not None
        assert set(verdict) == {"systemMessage"}
        assert "could not run the estimate check" in verdict["systemMessage"]
        assert "Set total to 2" in verdict["systemMessage"]


def test_a_fixed_estimate_clears_the_count(tmp_path):
    check_estimate(tmp_path, estimator_stop(tmp_path, estimate_report(total=3)))
    assert check_estimate(tmp_path, estimator_stop(tmp_path, estimate_report())) is None
    assert not (tmp_path / "home" / "estimate-check" / "s1-a1.json").exists()
    verdict = check_estimate(tmp_path, estimator_stop(tmp_path, estimate_report(total=3)))
    assert verdict is not None
    assert verdict["decision"] == "block"


@pytest.mark.parametrize("agent", ["agent-harness:implementer", "Explore", ""])
def test_other_agents_are_not_checked(tmp_path, agent):
    invalid = estimate_report(total=3)
    assert check_estimate(tmp_path, estimator_stop(tmp_path, invalid, agent)) is None


def test_a_reply_without_an_estimate_is_not_checked(tmp_path):
    reply = "Added to memory:\n- Same ticket: 4 h holds when trimmed (source: retro, 2026-01-05)"
    assert check_estimate(tmp_path, estimator_stop(tmp_path, reply, "estimator")) is None


def estimator_transcript(tmp_path: Path, *content: dict[str, Any]) -> Path:
    path = tmp_path / "subagents" / "agent-a1.jsonl"
    path.parent.mkdir(exist_ok=True)
    lines = [
        json.dumps({"type": "user", "message": {"content": "Estimate SHOP-412"}}),
        json.dumps({"type": "assistant", "message": {"content": list(content)}}),
        "not json",
    ]
    path.write_text("\n".join(lines) + "\n")
    return path


def handback(report: str) -> dict[str, Any]:
    return {"type": "tool_use", "name": "SubagentHandback", "input": {"message": report}}


def test_a_handed_back_report_is_checked_but_only_warned_about(tmp_path):
    transcript = estimator_transcript(tmp_path, handback(estimate_report(total=3)))
    event = estimator_stop(tmp_path, "Done.", agent_transcript_path=str(transcript))

    verdict = check_estimate(tmp_path, event)

    assert verdict is not None
    assert "decision" not in verdict
    assert "handed back already" in verdict["systemMessage"]
    record = json.loads((tmp_path / "home" / "estimate-check" / "s1-a1.json").read_text())
    assert record["handed_back"] is True


def test_a_valid_handed_back_report_passes(tmp_path):
    transcript = estimator_transcript(tmp_path, handback(estimate_report()))
    event = estimator_stop(tmp_path, "", agent_transcript_path=str(transcript))
    assert check_estimate(tmp_path, event) is None


def test_without_the_last_message_the_transcript_is_read(tmp_path):
    text = {"type": "text", "text": estimate_report(total=3)}
    transcript = estimator_transcript(tmp_path, text)
    event = estimator_stop(tmp_path, None, agent_transcript_path=str(transcript))

    verdict = check_estimate(tmp_path, event)

    assert verdict is not None
    assert verdict["decision"] == "block"


def estimator_handback(report: Any, tool: str = "SubagentHandback") -> dict[str, Any]:
    return {
        "hook_event_name": "PreToolUse",
        "session_id": "s1",
        "agent_id": "a1",
        "agent_type": "agent-harness:estimator",
        "tool_name": tool,
        "tool_input": {"message": report},
    }


def handback_denial(verdict: dict[str, Any] | None) -> str:
    assert verdict is not None
    output = verdict["hookSpecificOutput"]
    assert (output["hookEventName"], output["permissionDecision"]) == ("PreToolUse", "deny")
    return str(output["permissionDecisionReason"])


def test_an_invalid_handback_is_denied(tmp_path):
    reason = handback_denial(check_estimate(tmp_path, estimator_handback(estimate_report(total=3))))

    assert "Set total to 2" in reason
    assert "Call SubagentHandback again with the whole report" in reason


@pytest.mark.parametrize(
    "event",
    [
        estimator_handback(estimate_report()),
        estimator_handback(estimate_report(total=3), tool="Write"),
        estimator_handback("Added to memory: nothing to estimate"),
        estimator_handback(None),
        {**estimator_handback(estimate_report(total=3)), "agent_type": "implementer"},
    ],
)
def test_other_handbacks_go_through(tmp_path, event):
    assert check_estimate(tmp_path, event) is None


def test_handback_and_stop_share_the_count(tmp_path):
    invalid = estimate_report(total=3)
    handback_denial(check_estimate(tmp_path, estimator_handback(invalid)))

    stopped = check_estimate(tmp_path, estimator_stop(tmp_path, invalid))
    assert stopped is not None
    assert stopped["decision"] == "block"
    final = check_estimate(tmp_path, estimator_stop(tmp_path, invalid, stop_hook_active=True))
    assert final is not None
    assert "after 2 retries" in final["systemMessage"]


def test_a_handback_let_through_after_two_denials_is_not_checked_at_stop(tmp_path):
    invalid = estimate_report(total=3)
    verdicts = [check_estimate(tmp_path, estimator_handback(invalid)) for _ in range(3)]

    handback_denial(verdicts[0])
    handback_denial(verdicts[1])
    assert verdicts[2] is not None
    assert "after 2 retries" in verdicts[2]["systemMessage"]
    transcript = estimator_transcript(tmp_path, handback(invalid))
    stop = estimator_stop(tmp_path, "Done.", agent_transcript_path=str(transcript))
    assert check_estimate(tmp_path, stop) is None
    assert not (tmp_path / "home" / "estimate-check" / "s1-a1.json").exists()
    assert check_estimate(tmp_path, stop) is not None


def test_a_valid_handback_is_not_checked_again_at_stop(tmp_path):
    assert check_estimate(tmp_path, estimator_handback(estimate_report())) is None
    record = json.loads((tmp_path / "home" / "estimate-check" / "s1-a1.json").read_text())
    assert record == {"blocks": 0, "errors": [], "handed_back": True, "delivered": True}

    assert check_estimate(tmp_path, estimator_stop(tmp_path, estimate_report(total=3))) is None
    assert not (tmp_path / "home" / "estimate-check" / "s1-a1.json").exists()


@pytest.mark.parametrize("payload", ["not json", "[1, 2]"])
def test_bad_input_never_blocks_the_estimator(tmp_path, payload):
    result = subprocess.run(  # noqa: S603
        [sys.executable, str(HOOKS / "estimate_check.py")],
        input=payload,
        capture_output=True,
        text=True,
        env={**os.environ, "HARNESS_HOME": str(tmp_path)},
        check=False,
    )
    assert result.returncode == 0
    assert result.stdout == ""
