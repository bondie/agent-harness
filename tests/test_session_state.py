import importlib.util
import json
import os
import subprocess
import sys
from io import StringIO
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
HOOKS = ROOT / "hooks"


def load(name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, HOOKS / f"{name}.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    sys.path.insert(0, str(HOOKS))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(HOOKS))
    return module


session_state = load("session_state")

START = 1_000_000.0


@pytest.fixture
def home(tmp_path: Path) -> Path:
    return tmp_path / "harness"


@pytest.fixture
def environ(home: Path) -> dict[str, str]:
    return {"HARNESS_HOME": str(home)}


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "shop"
    (root / ".git").mkdir(parents=True)
    (root / ".git" / "HEAD").write_text("ref: refs/heads/feature/SHOP-1\n", encoding="utf-8")
    (root / "src" / "client").mkdir(parents=True)
    return root


@pytest.fixture(autouse=True)
def no_ps(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(session_state, "claude_pid", lambda: os.getpid())


def event(name: str, repo: Path, **extra: Any) -> dict[str, Any]:
    return {"hook_event_name": name, "session_id": "s-1", "cwd": str(repo), **extra}


def state_file(home: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((home / "sessions" / "s-1.json").read_text(encoding="utf-8"))
    return data


@pytest.mark.parametrize(
    ("hook", "extra", "expected"),
    [
        ("SessionStart", {"source": "startup"}, ("idle", None)),
        ("UserPromptSubmit", {"prompt": "fix it"}, ("working", None)),
        ("PreToolUse", {"tool_name": "Bash"}, ("working", "Bash")),
        ("PostToolUse", {"tool_name": "Read"}, ("working", "Read")),
        ("PostToolUseFailure", {"tool_name": "Bash"}, ("working", "Bash")),
        ("PreToolUse", {"tool_name": "AskUserQuestion"}, ("waiting_answer", "AskUserQuestion")),
        ("PreToolUse", {"tool_name": "ExitPlanMode"}, ("waiting_answer", "ExitPlanMode")),
        (
            "Notification",
            {"notification_type": "permission_prompt", "message": "Claude needs your permission"},
            ("waiting_permission", "Claude needs your permission"),
        ),
        ("Notification", {"notification_type": "elicitation_dialog"}, ("waiting_answer", None)),
        ("Stop", {}, ("idle", None)),
    ],
)
def test_each_event_sets_its_state(hook, extra, expected, repo, home, environ):
    session_state.record(event(hook, repo, **extra), environ, now=START)

    data = state_file(home)
    assert (data["state"], data["detail"]) == expected
    assert data["project"] == "shop"
    assert data["root"] == str(repo)
    assert data["branch"] == "feature/SHOP-1"
    assert data["version"] == 1
    assert data["pid"] == os.getpid()


def test_an_idle_prompt_keeps_an_idle_session_as_it_was(repo, home, environ):
    session_state.record(event("Stop", repo), environ, now=START)
    session_state.record(
        event("Notification", repo, notification_type="idle_prompt"), environ, now=START + 60
    )

    assert state_file(home)["state"] == "idle"
    assert state_file(home)["state_since"] == session_state.stamp(START)


def test_an_idle_prompt_ends_a_turn_interrupted_without_stop(repo, home, environ):
    session_state.record(event("PreToolUse", repo, tool_name="Bash"), environ, now=START)
    session_state.record(
        event("Notification", repo, notification_type="idle_prompt"), environ, now=START + 60
    )

    assert (state_file(home)["state"], state_file(home)["detail"]) == ("idle", None)


def test_an_idle_prompt_does_not_hide_an_open_question(repo, home, environ):
    session_state.record(event("PreToolUse", repo, tool_name="AskUserQuestion"), environ, now=START)
    session_state.record(
        event("Notification", repo, notification_type="idle_prompt"), environ, now=START + 90
    )

    assert state_file(home)["state"] == "waiting_answer"


def test_a_compaction_keeps_the_state_of_a_working_session(repo, home, environ):
    session_state.record(event("PreToolUse", repo, tool_name="Bash"), environ, now=START)
    session_state.record(event("SessionStart", repo, source="compact"), environ, now=START + 9)

    data = state_file(home)
    assert (data["state"], data["detail"]) == ("working", "Bash")
    assert data["state_since"] == session_state.stamp(START)


def test_a_compaction_of_an_unknown_session_starts_idle(repo, home, environ):
    session_state.record(event("SessionStart", repo, source="compact"), environ, now=START)

    assert state_file(home)["state"] == "idle"


def test_subagent_tool_calls_leave_a_working_or_idle_session_alone(repo, home, environ):
    session_state.record(
        event("PreToolUse", repo, tool_name="Bash", agent_id="a-1"), environ, now=START
    )
    assert not (home / "sessions").exists()
    session_state.record(event("Stop", repo), environ, now=START + 1)
    session_state.record(
        event("PostToolUse", repo, tool_name="Bash", agent_id="a-1"), environ, now=START + 5
    )

    assert state_file(home)["state"] == "idle"


def test_a_subagent_permission_prompt_waits_and_its_next_call_ends_the_wait(repo, home, environ):
    session_state.record(event("PreToolUse", repo, tool_name="Agent"), environ, now=START)
    prompt = event(
        "Notification", repo, notification_type="permission_prompt", message="Bash", agent_id="a-1"
    )
    session_state.record(prompt, environ, now=START + 10)
    waiting = state_file(home)
    session_state.record(
        event("PostToolUse", repo, tool_name="Read", agent_id="a-2"), environ, now=START + 12
    )
    still = state_file(home)["state"]
    session_state.record(
        event("PostToolUse", repo, tool_name="Bash", agent_id="a-1"), environ, now=START + 30
    )

    assert (waiting["state"], waiting["waiting_agent"]) == ("waiting_permission", "a-1")
    assert waiting["project"] == "shop"
    assert still == "waiting_permission"
    assert (state_file(home)["state"], state_file(home)["waiting_agent"]) == ("working", None)


def test_a_subagent_call_ends_a_wait_whose_origin_is_unknown(repo, home, environ):
    session_state.record(
        event("Notification", repo, notification_type="permission_prompt"), environ, now=START
    )
    session_state.record(
        event("PostToolUse", repo, tool_name="Bash", agent_id="a-1"), environ, now=START + 30
    )

    assert state_file(home)["state"] == "working"


def test_a_subagent_session_end_keeps_the_file(repo, home, environ):
    session_state.record(event("Stop", repo), environ, now=START)
    session_state.record(event("SessionEnd", repo, agent_id="a-1"), environ, now=START + 1)

    assert (home / "sessions" / "s-1.json").exists()


def test_session_end_removes_the_file(repo, home, environ):
    session_state.record(event("UserPromptSubmit", repo), environ, now=START)
    session_state.record(event("SessionEnd", repo, reason="exit"), environ, now=START + 1)

    assert not (home / "sessions" / "s-1.json").exists()


def test_state_since_keeps_while_the_state_stays_and_moves_when_it_changes(repo, home, environ):
    session_state.record(event("UserPromptSubmit", repo), environ, now=START)
    session_state.record(event("PreToolUse", repo, tool_name="Bash"), environ, now=START + 30)
    working = state_file(home)
    session_state.record(event("Stop", repo), environ, now=START + 90)
    idle = state_file(home)

    assert working["state_since"] == session_state.stamp(START)
    assert working["detail"] == "Bash"
    assert idle["state_since"] == session_state.stamp(START + 90)
    assert idle["started_at"] == session_state.stamp(START)


def test_a_repeated_state_within_two_seconds_is_not_rewritten(repo, home, environ, monkeypatch):
    session_state.record(event("PreToolUse", repo, tool_name="Bash"), environ, now=START)
    writes: list[str] = []
    monkeypatch.setattr(session_state, "write", lambda path, data: writes.append(path))
    session_state.record(event("PostToolUse", repo, tool_name="Bash"), environ, now=START + 1)
    session_state.record(event("PostToolUse", repo, tool_name="Bash"), environ, now=START + 3)

    assert len(writes) == 1


def test_tool_events_keep_the_project_without_reading_git_again(repo, home, environ, monkeypatch):
    session_state.record(event("UserPromptSubmit", repo), environ, now=START)
    monkeypatch.setattr(session_state, "git_head", lambda cwd: pytest.fail("read git"))
    session_state.record(event("PreToolUse", repo, tool_name="Grep"), environ, now=START + 5)

    assert state_file(home)["project"] == "shop"


def test_the_project_comes_from_the_repository_root_and_a_worktree_file(tmp_path, environ):
    main = tmp_path / "main"
    (main / ".git" / "worktrees" / "wt").mkdir(parents=True)
    (main / ".git" / "worktrees" / "wt" / "HEAD").write_text("ref: refs/heads/topic\n")
    tree = tmp_path / "wt"
    (tree / "deep").mkdir(parents=True)
    (tree / ".git").write_text(f"gitdir: {main / '.git' / 'worktrees' / 'wt'}\n")
    (main / ".git" / "worktrees" / "wt" / "commondir").write_text("../..\n")

    assert session_state.git_head(str(tree / "deep")) == (str(tree), "main", "topic")


def test_a_detached_head_has_no_branch(repo):
    (repo / ".git" / "HEAD").write_text("0123456789abcdef\n", encoding="utf-8")

    assert session_state.git_head(str(repo / "src")) == (str(repo), "shop", None)


def test_outside_a_repository_the_project_is_the_folder(tmp_path, home, environ):
    folder = tmp_path / "notes"
    folder.mkdir()
    session_state.record(
        {"hook_event_name": "SessionStart", "session_id": "s-1", "cwd": str(folder)},
        environ,
        now=START,
    )

    assert state_file(home)["project"] == "notes"
    assert state_file(home)["branch"] is None


@pytest.mark.parametrize("session", ["", "../x", "a/b", ".hidden", None, 7])
def test_an_unsafe_session_id_writes_nothing(session, repo, home, environ):
    session_state.record(
        {"hook_event_name": "Stop", "session_id": session, "cwd": str(repo)}, environ, now=START
    )

    assert not (home / "sessions").exists()


def test_session_start_removes_files_of_dead_sessions(repo, home, environ):
    sessions = home / "sessions"
    sessions.mkdir(parents=True)
    (sessions / "dead.json").write_text(json.dumps({"pid": 2**22 + 12345}))
    (sessions / "live.json").write_text(json.dumps({"pid": os.getpid()}))
    unknown = {"pid": None, "updated_epoch": START - 3600}
    (sessions / "unknown.json").write_text(json.dumps(unknown))
    stale = {"pid": None, "updated_epoch": START - 2 * 24 * 3600}
    (sessions / "stale.json").write_text(json.dumps(stale))
    session_state.record(event("SessionStart", repo), environ, now=START)

    names = sorted(path.name for path in sessions.glob("*.json"))
    assert names == ["live.json", "s-1.json", "unknown.json"]


def test_a_failed_process_lookup_is_not_repeated(repo, home, environ, monkeypatch):
    calls: list[int] = []
    monkeypatch.setattr(session_state, "claude_pid", lambda: calls.append(1))
    session_state.record(event("UserPromptSubmit", repo), environ, now=START)
    session_state.record(event("Stop", repo), environ, now=START + 10)

    assert calls == [1]
    assert state_file(home)["pid"] is None


def test_the_write_is_atomic_and_leaves_no_temporary_file(repo, home, environ):
    session_state.record(event("Stop", repo), environ, now=START)

    assert [path.name for path in (home / "sessions").iterdir()] == ["s-1.json"]


def test_main_never_fails(home, environ):
    assert session_state.main(StringIO("not json"), environ) == 0
    assert session_state.main(StringIO("[]"), environ) == 0
    assert not (home / "sessions").exists()


def test_the_runaway_guard_records_main_session_tool_calls(repo, home, environ):
    payload = json.dumps(event("PreToolUse", repo, tool_name="Edit", tool_input={}))
    done = subprocess.run(  # noqa: S603
        [sys.executable, str(HOOKS / "guard_runaway.py")],
        input=payload,
        capture_output=True,
        text=True,
        env={**os.environ, **environ},
        check=False,
    )

    assert done.returncode == 0
    assert done.stdout == ""
    data = state_file(home)
    assert (data["state"], data["detail"], data["project"]) == ("working", "Edit", "shop")


def test_the_runaway_guard_passes_a_subagent_call_that_creates_no_state(repo, home, environ):
    payload = json.dumps(event("PreToolUse", repo, tool_name="Edit", tool_input={}, agent_id="a-1"))
    subprocess.run(  # noqa: S603
        [sys.executable, str(HOOKS / "guard_runaway.py")],
        input=payload,
        capture_output=True,
        text=True,
        env={**os.environ, **environ},
        check=False,
    )

    assert not (home / "sessions").exists()


def test_the_hook_is_registered_for_its_events():
    hooks = json.loads((HOOKS / "hooks.json").read_text(encoding="utf-8"))["hooks"]
    for name in ("SessionStart", "UserPromptSubmit", "Notification", "Stop", "SessionEnd"):
        commands = [hook["command"] for entry in hooks[name] for hook in entry["hooks"]]
        assert any("session_state.py" in command for command in commands), name


def agent_file(home: Path, agent: str = "a-1") -> Path:
    return home / "agents" / "s-1" / f"{agent}.json"


def subagent(name: str, repo: Path, agent: str = "a-1", **extra: Any) -> dict[str, Any]:
    return event(name, repo, agent_id=agent, agent_type="agent-harness:reviewer-qa", **extra)


def test_a_subagent_file_lives_from_start_to_stop_and_counts_its_calls(repo, home, environ):
    session_state.record(subagent("SubagentStart", repo), environ, now=START)
    session_state.record(subagent("PreToolUse", repo, tool_name="Read"), environ, now=START + 5)
    session_state.record(subagent("PostToolUse", repo, tool_name="Read"), environ, now=START + 6)
    session_state.record(subagent("PreToolUse", repo, tool_name="Bash"), environ, now=START + 9)
    running = json.loads(agent_file(home).read_text(encoding="utf-8"))
    session_state.record(subagent("SubagentStop", repo), environ, now=START + 30)

    assert running["agent_type"] == "agent-harness:reviewer-qa"
    assert running["started_epoch"] == START
    assert (running["tool_calls"], running["last_tool"]) == (2, "Bash")
    assert running["updated_epoch"] == START + 9
    assert not agent_file(home).exists()


def test_task_stop_of_the_main_session_marks_the_agent_until_it_runs_again(repo, home, environ):
    session_state.record(subagent("SubagentStart", repo), environ, now=START)
    session_state.record(subagent("SubagentStart", repo, agent="a-2"), environ, now=START)
    stop = event("PostToolUse", repo, tool_name="TaskStop", tool_input={"task_id": "a-1"})
    session_state.record(stop, environ, now=START + 30)
    stopped = json.loads(agent_file(home).read_text(encoding="utf-8"))
    other = json.loads(agent_file(home, "a-2").read_text(encoding="utf-8"))
    session_state.record(subagent("PreToolUse", repo, tool_name="Read"), environ, now=START + 60)
    resumed = json.loads(agent_file(home).read_text(encoding="utf-8"))

    assert stopped["stopped_at"] == session_state.stamp(START + 30)
    assert stopped["updated_epoch"] == START + 30
    assert "stopped_at" not in other
    assert "stopped_at" not in resumed


def test_task_stop_of_a_shell_or_an_unsafe_id_leaves_the_agents(repo, home, environ):
    session_state.record(subagent("SubagentStart", repo), environ, now=START)
    for task in ("b-shell", "../a-1", None):
        stop = event("PostToolUse", repo, tool_name="TaskStop", tool_input={"task_id": task})
        session_state.record(stop, environ, now=START + 30)

    assert "stopped_at" not in json.loads(agent_file(home).read_text(encoding="utf-8"))
    assert not (home / "agents" / "s-1" / "b-shell.json").exists()


def test_a_failed_or_a_subagents_task_stop_marks_nothing(repo, home, environ):
    session_state.record(subagent("SubagentStart", repo), environ, now=START)
    session_state.record(subagent("SubagentStart", repo, agent="a-2"), environ, now=START)
    failed = event("PostToolUseFailure", repo, tool_name="TaskStop", tool_input={"task_id": "a-1"})
    session_state.record(failed, environ, now=START + 30)
    own = subagent("PostToolUse", repo, tool_name="TaskStop", tool_input={"task_id": "a-2"})
    session_state.record(own, environ, now=START + 30)

    assert "stopped_at" not in json.loads(agent_file(home).read_text(encoding="utf-8"))
    assert "stopped_at" not in json.loads(agent_file(home, "a-2").read_text(encoding="utf-8"))


def test_a_tool_call_of_an_unknown_subagent_creates_no_file(repo, home, environ):
    session_state.record(subagent("PreToolUse", repo, tool_name="Read"), environ, now=START)

    assert not (home / "agents").exists()


def test_parallel_subagents_keep_their_own_files(repo, home, environ):
    for agent in ("a-1", "a-2"):
        session_state.record(subagent("SubagentStart", repo, agent=agent), environ, now=START)
    session_state.record(
        subagent("PreToolUse", repo, agent="a-2", tool_name="Grep"), environ, now=START + 1
    )

    first = json.loads(agent_file(home, "a-1").read_text(encoding="utf-8"))
    second = json.loads(agent_file(home, "a-2").read_text(encoding="utf-8"))
    assert (first["tool_calls"], second["tool_calls"]) == (0, 1)


def test_session_start_removes_the_agents_of_ended_sessions(repo, home, environ):
    session_state.record(subagent("SubagentStart", repo), environ, now=START)
    sessions = home / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    (sessions / "gone.json").write_text(json.dumps({"pid": 2**22 + 12345}))
    gone = home / "agents" / "gone"
    gone.mkdir(parents=True)
    (gone / "a-9.json").write_text(json.dumps({"updated_epoch": START}))
    unknown = home / "agents" / "no-session-file"
    unknown.mkdir(parents=True)
    (unknown / "a-8.json").write_text(json.dumps({"updated_epoch": START}))
    forgotten = home / "agents" / "forgotten"
    forgotten.mkdir(parents=True)
    (forgotten / "a-7.json").write_text(json.dumps({"updated_epoch": START - 2 * 24 * 3600}))
    session_state.record(event("SessionStart", repo), environ, now=START + 1)

    assert agent_file(home).exists()
    assert not gone.exists()
    assert unknown.exists()
    assert not forgotten.exists()


def test_the_runaway_guard_counts_a_subagent_call(repo, home, environ):
    session_state.record(subagent("SubagentStart", repo), environ, now=START)
    payload = json.dumps(subagent("PreToolUse", repo, tool_name="Edit", tool_input={}))
    subprocess.run(  # noqa: S603
        [sys.executable, str(HOOKS / "guard_runaway.py")],
        input=payload,
        capture_output=True,
        text=True,
        env={**os.environ, **environ},
        check=False,
    )

    assert json.loads(agent_file(home).read_text(encoding="utf-8"))["tool_calls"] == 1


def test_the_hook_is_registered_for_subagent_start_and_stop():
    hooks = json.loads((HOOKS / "hooks.json").read_text(encoding="utf-8"))["hooks"]
    for name in ("SubagentStart", "SubagentStop"):
        commands = [hook["command"] for entry in hooks[name] for hook in entry["hooks"]]
        assert any("session_state.py" in command for command in commands), name


def work_lines(home: Path) -> list[dict[str, Any]]:
    path = home / "work.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


@pytest.mark.parametrize(
    "prompt",
    [
        "/agent-harness:ticket SHOP-12",
        "  /ticket SHOP-12 and estimate it",
        "/agent-harness:work SHOP-12",
        "/agent-harness:ticket UTF-8 bug in SHOP-12",
        "<command-message>agent-harness:ticket</command-message>\n"
        "<command-name>/agent-harness:ticket</command-name>\n<command-args>SHOP-12</command-args>",
    ],
)
def test_a_typed_ticket_or_work_command_marks_the_ticket(repo, home, environ, prompt):
    typed = event("UserPromptSubmit", repo, prompt=prompt, transcript_path="/t/s-1.jsonl")
    session_state.record(typed, environ, now=START)

    assert work_lines(home) == [
        {
            "session": "s-1",
            "ticket": "SHOP-12",
            "at": session_state.stamp(START),
            "transcript": "/t/s-1.jsonl",
        }
    ]
    assert state_file(home)["ticket"] == "SHOP-12"


@pytest.mark.parametrize(
    "prompt",
    [
        "look at SHOP-12",
        "what does /agent-harness:ticket SHOP-12 do?",
        "/agent-harness:stats SHOP-12",
        "/agent-harness:ticket",
    ],
)
def test_other_prompts_mark_nothing(repo, home, environ, prompt):
    session_state.record(event("UserPromptSubmit", repo, prompt=prompt), environ, now=START)

    assert not (home / "work.jsonl").exists()
    assert "ticket" not in state_file(home)


def test_work_stop_ends_the_ticket(repo, home, environ):
    start = event("UserPromptSubmit", repo, prompt="/agent-harness:work SHOP-12")
    session_state.record(start, environ, now=START)
    stop = event("UserPromptSubmit", repo, prompt="/agent-harness:work stop SHOP-12")
    session_state.record(stop, environ, now=START + 1)
    called = {"skill": "agent-harness:work", "args": "Stop"}
    session_state.record(
        event("PreToolUse", repo, tool_name="Skill", tool_input=called), environ, now=START + 2
    )

    assert [line["ticket"] for line in work_lines(home)] == ["SHOP-12", None, None]
    assert state_file(home)["ticket"] is None


def test_the_main_sessions_skill_call_marks_the_ticket_once(repo, home, environ):
    tool_input = {"skill": "agent-harness:ticket", "args": "SHOP-12"}
    for name in ("PreToolUse", "PostToolUse"):
        call = event(name, repo, tool_name="Skill", tool_input=tool_input)
        session_state.record(call, environ, now=START)

    assert [line["ticket"] for line in work_lines(home)] == ["SHOP-12"]


@pytest.mark.parametrize("skill", ["ticket", "work", "other:ticket"])
def test_a_skill_call_of_another_ticket_or_work_skill_marks_nothing(repo, home, environ, skill):
    call = event("PreToolUse", repo, tool_name="Skill", tool_input={"skill": skill, "args": "X-1"})
    session_state.record(call, environ, now=START)

    assert not (home / "work.jsonl").exists()


def test_a_subagents_skill_call_marks_nothing(repo, home, environ):
    tool_input = {"skill": "agent-harness:ticket", "args": "SHOP-12"}
    session_state.record(
        subagent("PreToolUse", repo, tool_name="Skill", tool_input=tool_input), environ, now=START
    )

    assert not (home / "work.jsonl").exists()
