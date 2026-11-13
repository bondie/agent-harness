import importlib.util
import json
import os
import stat
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
bridge = load("permission_bridge")


@pytest.fixture
def home(tmp_path: Path) -> Path:
    return tmp_path / "harness"


@pytest.fixture
def environ(home: Path) -> dict[str, str]:
    return {"HARNESS_HOME": str(home)}


@pytest.fixture(autouse=True)
def no_ps(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(session_state, "claude_pid", lambda: os.getpid())


def enable(home: Path, **extra: Any) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "guard.json").write_text(json.dumps({"permission_bridge": {"enabled": True, **extra}}))


def request(**extra: Any) -> dict[str, Any]:
    return {
        "hook_event_name": "PermissionRequest",
        "session_id": "s-1",
        "cwd": "/work/shop",
        "tool_name": "Bash",
        "tool_input": {"command": "rm -rf  build", "description": "Clean"},
        **extra,
    }


class Clock:
    """A fake clock whose sleep runs a callback, standing in for the viewer."""

    def __init__(self, act: Any = None) -> None:
        self.now = 0.0
        self.act = act
        self.slept = 0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept += 1
        self.now += seconds
        if self.act is not None:
            self.act(self)


def pending(home: Path) -> list[Path]:
    folder = home / "permissions"
    return [path for path in folder.glob("*.json") if not path.name.endswith(".decision.json")]


def test_it_does_nothing_unless_enabled(home, environ):
    assert bridge.bridge(request(), environ) is None
    assert not (home / "permissions").exists()


def decide(home: Path, decision: dict[str, Any]) -> Any:
    def act(clock: Clock) -> None:
        if clock.slept == 2:
            path = pending(home)[0]
            folder = path.parent
            assert stat.S_IMODE(folder.stat().st_mode) == 0o700
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
            data = json.loads(path.read_text())
            assert (data["tool_name"], data["summary"]) == ("Bash", "Bash: rm -rf build")
            (folder / f"{data['id']}.decision.json").write_text(json.dumps(decision))

    return act


def test_an_allow_from_the_viewer_answers_the_prompt(home, environ):
    enable(home)
    clock = Clock(decide(home, {"behavior": "allow"}))

    verdict = bridge.bridge(request(), environ, clock=clock, sleep=clock.sleep)

    assert verdict == {
        "hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": {"behavior": "allow"},
        }
    }
    assert list((home / "permissions").iterdir()) == []


def test_a_deny_carries_its_message_or_a_default(home, environ):
    enable(home)
    clock = Clock(decide(home, {"behavior": "deny"}))

    verdict = bridge.bridge(request(), environ, clock=clock, sleep=clock.sleep)

    decision = verdict["hookSpecificOutput"]["decision"]
    assert decision == {"behavior": "deny", "message": bridge.DENIED}


def test_an_answer_in_the_terminal_ends_the_wait(home, environ):
    enable(home)
    event = request()

    def act(clock: Clock) -> None:
        if clock.slept == 3:
            done = dict(event, hook_event_name="PostToolUse", tool_response={})
            session_state.record(done, environ)

    clock = Clock(act)
    assert bridge.bridge(event, environ, clock=clock, sleep=clock.sleep) is None
    assert clock.slept == 3


def test_a_parallel_call_does_not_end_the_wait_but_the_end_of_the_turn_does(home, environ):
    enable(home)
    event = request()

    def act(clock: Clock) -> None:
        if clock.slept == 1:
            other = dict(event, hook_event_name="PostToolUse", tool_name="Read", tool_input={})
            session_state.record(other, environ)
        if clock.slept == 4:
            session_state.record(dict(event, hook_event_name="Stop"), environ)

    clock = Clock(act)
    assert bridge.bridge(event, environ, clock=clock, sleep=clock.sleep) is None
    assert clock.slept == 4


def test_another_session_does_not_end_the_wait(home, environ):
    enable(home, wait_seconds=1, poll_seconds=0.25)

    def act(clock: Clock) -> None:
        session_state.record({"hook_event_name": "Stop", "session_id": "s-2"}, environ)

    clock = Clock(act)
    assert bridge.bridge(request(), environ, clock=clock, sleep=clock.sleep) is None
    assert clock.slept == 4
    assert pending(home) == []


def test_a_decision_through_a_symlink_is_ignored(home, environ, tmp_path):
    enable(home, wait_seconds=1, poll_seconds=0.5)
    elsewhere = tmp_path / "allow.json"
    elsewhere.write_text(json.dumps({"behavior": "allow"}))

    def act(clock: Clock) -> None:
        if clock.slept == 1:
            path = pending(home)[0]
            (path.parent / f"{path.stem}.decision.json").symlink_to(elsewhere)

    clock = Clock(act)
    assert bridge.bridge(request(), environ, clock=clock, sleep=clock.sleep) is None


def test_a_malformed_decision_is_ignored_until_the_timeout(home, environ):
    enable(home, wait_seconds=1, poll_seconds=0.5)

    def act(clock: Clock) -> None:
        path = pending(home)[0]
        (path.parent / f"{path.stem}.decision.json").write_text('{"behavior": "maybe"}')

    clock = Clock(act)
    assert bridge.bridge(request(), environ, clock=clock, sleep=clock.sleep) is None


def test_main_leaves_the_prompt_to_the_terminal_on_bad_input(environ):
    out = StringIO()
    assert bridge.main(StringIO("not json"), out, environ) == 0
    assert out.getvalue() == ""


def test_the_session_waits_at_once_with_the_exact_command(home, environ):
    session_state.record(request(), environ, now=1000.0)
    session_state.record(
        {
            "hook_event_name": "Notification",
            "session_id": "s-1",
            "notification_type": "permission_prompt",
            "message": "Claude needs your permission to use Bash",
        },
        environ,
        now=1006.0,
    )

    data = json.loads((home / "sessions" / "s-1.json").read_text())
    assert (data["state"], data["detail"]) == ("waiting_permission", "Bash: rm -rf build")


def test_a_subagent_permission_request_waits_for_that_agent(home, environ):
    session_state.record(request(agent_id="a-1"), environ, now=1000.0)

    data = json.loads((home / "sessions" / "s-1.json").read_text())
    assert (data["state"], data["waiting_agent"]) == ("waiting_permission", "a-1")


@pytest.mark.parametrize(
    ("tool", "tool_input", "expected"),
    [
        ("Edit", {"file_path": "/work/a.php", "old_string": "x"}, "Edit: /work/a.php"),
        (
            "WebFetch",
            {"url": "https://example.com", "prompt": "x"},
            "WebFetch: https://example.com",
        ),
        ("mcp__jira__add_comment", {"issueKey": "SHOP-1"}, "mcp__jira__add_comment"),
        ("Bash", "not a dict", "Bash"),
    ],
)
def test_describe(tool, tool_input, expected):
    assert session_state.describe(tool, tool_input) == expected


def test_the_hooks_are_registered_for_permission_requests():
    hooks = json.loads((HOOKS / "hooks.json").read_text(encoding="utf-8"))["hooks"]
    commands = [hook["command"] for entry in hooks["PermissionRequest"] for hook in entry["hooks"]]
    assert any("permission_bridge.py" in command for command in commands)
    assert any("session_state.py" in command for command in commands)


def test_state_files_are_private_even_with_the_bridge_off(home, environ):
    session_state.record(request(tool_input={"command": "mysql -pSecret"}), environ, now=1000.0)

    path = home / "sessions" / "s-1.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_wait_seconds_stays_under_the_hook_timeout(home, environ):
    enable(home, wait_seconds=99999)

    assert bridge.settings(environ)["wait_seconds"] == bridge.LONGEST_WAIT


def test_session_start_removes_requests_a_killed_hook_left(home, environ):
    folder = home / "permissions"
    folder.mkdir(parents=True)
    (folder / "old.json").write_text(json.dumps({"session_id": "s-9", "created_epoch": 0}))
    (folder / "fresh.json").write_text(json.dumps({"session_id": "s-9", "created_epoch": 9999.0}))
    session_state.record(
        {"hook_event_name": "SessionStart", "session_id": "s-1"}, environ, now=10000.0
    )

    assert sorted(path.name for path in folder.iterdir()) == ["fresh.json"]


def test_cleanup_keeps_the_decision_of_a_live_request(home, environ):
    folder = home / "permissions"
    folder.mkdir(parents=True)
    (folder / "r1.json").write_text(json.dumps({"session_id": "s-9", "created_epoch": 9999.0}))
    (folder / "r1.decision.json").write_text(json.dumps({"behavior": "allow"}))
    (folder / "gone.decision.json").write_text(json.dumps({"behavior": "allow"}))
    (folder / ".r2.decision.tmp").write_text("{}")
    session_state.record(
        {"hook_event_name": "SessionStart", "session_id": "s-1"}, environ, now=10000.0
    )

    names = sorted(path.name for path in folder.iterdir())
    assert names == [".r2.decision.tmp", "r1.decision.json", "r1.json"]
