import importlib.util
import json
import os
import subprocess
import sys
import time
from io import StringIO
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
HOOKS = ROOT / "hooks"
SCRIPT = HOOKS / "guard_runaway.py"


def load_hook() -> Any:
    spec = importlib.util.spec_from_file_location("guard_runaway", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["guard_runaway"] = module
    sys.path.insert(0, str(HOOKS))
    try:
        spec.loader.exec_module(module)
        for sibling in ("guard_shell", "save_artifacts"):
            importlib.import_module(sibling)
    finally:
        sys.path.remove(str(HOOKS))
    return module


guard_runaway = load_hook()

START = 1_000_000.0
DAY = 24 * 3600
SUBAGENT = {"agent_id": "agent-1", "agent_type": "agent-harness:implementer"}
Call = tuple[str, dict[str, Any]]
TEST: Call = ("Bash", {"command": "npm test", "description": "Run the tests"})
LINT: Call = ("Bash", {"command": "npm run lint"})
READ: Call = ("Read", {"file_path": "/work/shop/src/cart.py"})
GREP: Call = ("Grep", {"pattern": "checkout", "path": "/work/shop/src"})
EDIT: Call = ("Edit", {"file_path": "/work/shop/src/cart.py", "old_string": "a", "new_string": "b"})
SLEEP: Call = ("Bash", {"command": "sleep 30 && gh run view 42"})
AGENT: Call = ("Agent", {"prompt": "Review the diff", "subagent_type": "reviewer-qa"})
HANDBACK: Call = ("SubagentHandback", {"message": "Done."})
ASK: Call = ("AskUserQuestion", {"questions": []})


class Run:
    """The events of one agent against its own HARNESS_HOME, on a fake clock."""

    def __init__(
        self,
        home: Path,
        identity: dict[str, str] | None = None,
        session: str = "session-1",
        cwd: str = "/work/shop",
        **environ: str,
    ) -> None:
        self.home = home
        self.identity = SUBAGENT if identity is None else identity
        self.session = session
        self.cwd = cwd
        self.environ = {"HOME": "/work/home", "HARNESS_HOME": str(home), **environ}
        self.clock = START

    def send(self, name: str, call: Call, **fields: Any) -> dict[str, Any] | None:
        tool, tool_input = call
        event: dict[str, Any] = {"session_id": self.session, "cwd": self.cwd}
        event["hook_event_name"] = name
        event |= {"tool_name": tool, "tool_input": tool_input, **self.identity, **fields}
        verdict: dict[str, Any] | None = guard_runaway.decision(event, self.environ, self.clock)
        return verdict

    def pre(self, call: Call) -> str | None:
        verdict = self.send("PreToolUse", call)
        if verdict is None:
            return None
        assert set(verdict) == {"hookSpecificOutput"}
        output = verdict["hookSpecificOutput"]
        assert output["hookEventName"] == "PreToolUse"
        assert output["permissionDecision"] == "deny"
        reason: str = output["permissionDecisionReason"]
        return reason

    def context(self, name: str, call: Call, **fields: Any) -> str | None:
        verdict = self.send(name, call, **fields)
        if verdict is None:
            return None
        assert set(verdict) == {"hookSpecificOutput"}
        output = verdict["hookSpecificOutput"]
        assert set(output) == {"hookEventName", "additionalContext"}
        assert output["hookEventName"] == name
        text: str = output["additionalContext"]
        return text

    def post(self, call: Call = EDIT) -> str | None:
        return self.context("PostToolUse", call)

    def fail(
        self, call: Call = TEST, interrupt: bool | None = False, error: str = "Exit code 1\nFAILED"
    ) -> str | None:
        fields: dict[str, Any] = {"error": error, "duration_ms": 1200}
        if interrupt is not None:
            fields["is_interrupt"] = interrupt
        return self.context("PostToolUseFailure", call, **fields)

    def state_file(self) -> Path:
        return self.home / "runaway" / self.session / f"{self.identity['agent_id']}.json"


def denied(run: Run, calls: list[Call]) -> list[int]:
    return [i for i, call in enumerate(calls) if run.pre(call)]


def guard_events(home: Path) -> list[dict[str, Any]]:
    path = home / "guard-events.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


@pytest.mark.parametrize(
    ("calls", "expected"),
    [
        pytest.param([TEST, TEST, TEST], [2], id="third-repeat-denied"),
        pytest.param([TEST, TEST, TEST, TEST], [2, 3], id="later-repeats-denied-too"),
        pytest.param([TEST, TEST], [], id="two-repeats-allowed"),
        pytest.param([TEST, READ, TEST, GREP, READ, TEST], [5], id="reads-do-not-reset"),
        pytest.param([TEST, TEST, EDIT, TEST, TEST], [], id="edit-ends-the-run"),
        pytest.param([TEST, TEST, LINT, TEST], [], id="other-command-ends-the-run"),
        pytest.param([EDIT, READ, EDIT, GREP, EDIT], [4], id="own-repeats-count"),
        pytest.param([READ, GREP, READ, GREP, READ], [4], id="repeated-read-denied"),
        pytest.param([READ, EDIT, READ, LINT, READ], [], id="other-call-ends-read-run"),
        pytest.param([TEST, LINT, TEST, LINT, TEST, LINT], [], id="alternating-is-missed"),
        pytest.param(
            [
                ("Bash", {"command": "npm  test", "description": "a"}),
                ("Bash", {"command": "npm test", "description": "b"}),
                ("Bash", {"command": " npm\ttest "}),
            ],
            [2],
            id="bash-description-and-spaces-ignored",
        ),
        pytest.param(
            [("Bash", {"command": "grep -rn sleepy src"})] * 3, [2], id="sleep-only-as-a-word"
        ),
        pytest.param(
            [("Read", {"file_path": f"/work/{n}.py"}) for n in range(5)], [], id="new-inputs"
        ),
    ],
)
def test_loop(tmp_path, calls, expected):
    assert denied(Run(tmp_path), calls) == expected


@pytest.mark.parametrize(
    "call",
    [
        SLEEP,
        ("Bash", {"command": "gh run watch 42; wait"}),
        ("Bash", {"command": "until curl -s localhost; do sleep 2; done"}),
        ("TaskOutput", {"task_id": "b1"}),
        ("BashOutput", {"bash_id": "b1"}),
        ("Monitor", {"command": "tail -f log"}),
        ("ScheduleWakeup", {"seconds": 60}),
        AGENT,
        ("Task", {"prompt": "x"}),
        ("SendMessage", {"to": "lead", "message": "x"}),
        ASK,
        HANDBACK,
    ],
    ids=lambda call: f"{call[0]}-{call[1]}"[:40],
)
def test_waits_polls_and_agent_calls_never_count_and_end_runs(tmp_path, call):
    assert denied(Run(tmp_path / "alone"), [call] * 5) == []
    assert denied(Run(tmp_path / "command"), [TEST, TEST, call, TEST, TEST]) == []
    assert denied(Run(tmp_path / "read"), [READ, READ, call, READ, READ]) == []


def test_loop_reason_names_the_count_and_the_way_out(tmp_path):
    run = Run(tmp_path)
    reasons = [run.pre(TEST) for _ in range(3)]
    assert reasons[:2] == [None, None]
    assert reasons[2] == (
        "agent-harness runaway guard, rule loop: the call was repeated 3 times without a "
        "change. To proceed: change the approach, or stop and report to the lead what "
        "blocks you."
    )


def test_agents_count_separately(tmp_path):
    first = Run(tmp_path)
    second = Run(tmp_path, {"agent_id": "agent-2", "agent_type": "agent-harness:reviewer-qa"})
    assert [first.pre(TEST), second.pre(TEST), first.pre(TEST), second.pre(TEST)] == [None] * 4
    assert first.pre(TEST)
    assert second.pre(TEST)


@pytest.mark.parametrize(
    "identity",
    [{}, {"agent_type": "agent-harness:implementer"}, {"agent_id": ""}],
    ids=["main-session", "main-session-with-agent-flag", "empty-agent-id"],
)
def test_main_session_is_never_touched(tmp_path, identity):
    run = Run(tmp_path, identity)
    assert denied(run, [TEST] * 5) == []
    for _ in range(6):
        assert run.fail() is None
    assert run.pre(EDIT) is None
    run.clock += DAY
    assert run.post() is None
    assert not (tmp_path / "runaway").exists()
    assert guard_events(tmp_path) == []


def streak_denials(run: Run, steps: str) -> list[int]:
    """f fails, m fails without is_interrupt, i is interrupted, s succeeds; p, r, a call."""
    calls = {"r": HANDBACK, "a": ASK}
    result = []
    for i, step in enumerate(steps):
        call: Call = ("Bash", {"command": f"step {i}"})
        if step == "f":
            run.fail(call)
        if step == "m":
            run.fail(call, interrupt=None)
        if step == "i":
            run.fail(call, interrupt=True)
        if step == "s":
            run.post(call)
        if step in "pra" and run.pre(calls.get(step, call)):
            result.append(i)
    return result


@pytest.mark.parametrize(
    ("steps", "expected"),
    [
        pytest.param("fffffpp", [5], id="five-failures-deny-once"),
        pytest.param("ffffp", [], id="four-failures-allowed"),
        pytest.param("ffffsffffp", [], id="success-resets"),
        pytest.param("ffffip", [], id="interrupt-does-not-count"),
        pytest.param("ffffifp", [6], id="interrupt-does-not-reset"),
        pytest.param("mmmmmp", [5], id="missing-interrupt-flag-counts"),
        pytest.param("fffffppffffp", [5], id="deny-resets-the-streak"),
        pytest.param("fffffpfffffp", [5, 11], id="next-streak-denied-again"),
        pytest.param("fpfpfpfpfp", [9], id="calls-between-do-not-reset"),
        pytest.param("fffffrp", [], id="handback-never-denied-and-resets"),
        pytest.param("fffffap", [], id="question-never-denied-and-resets"),
    ],
)
def test_failure_streak(tmp_path, steps, expected):
    assert streak_denials(Run(tmp_path), steps) == expected


def test_failure_streak_reason_and_count(tmp_path):
    run = Run(tmp_path)
    for i in range(7):
        run.fail(("Bash", {"command": f"step {i}"}))
    assert run.pre(EDIT) == (
        "agent-harness runaway guard, rule failure_streak: 7 calls failed in a row. "
        "To proceed: if the failures were expected (no match, a test you expect to fail), go "
        "on; otherwise stop and report to the lead what fails and what you tried."
    )


@pytest.mark.parametrize(
    "command",
    [
        "grep -rn checkout src",
        "egrep -r 'cart|order' src",
        "fgrep -r checkout src",
        "rg checkout",
        "ag checkout",
        "diff old.txt new.txt",
        "cmp old.bin new.bin",
        "test -f build/out.txt",
        "[ -f build/out.txt ]",
        "[[ -f build/out.txt ]]",
        "git diff --exit-code",
        "git diff --quiet HEAD -- src",
        "git -C /work/shop grep checkout",
        "cat app.log | grep ERROR",
        "timeout 5 grep -rn checkout src",
        "grep -rn checkout src 2>/dev/null;",
        "! grep -q TODO src/cart.py",
        "/usr/bin/grep -rn checkout src",
        "cd /work/shop && grep -rn checkout src",
        "cd /work/shop && cd src && rg checkout",
        "pushd /work/shop && git grep checkout",
        "cd /work/shop && cat app.log | grep ERROR",
        "cd /work/shop && test -f build/out.txt",
    ],
)
def test_a_no_match_or_false_exit_is_not_a_failure(tmp_path, command):
    run = Run(tmp_path)
    for _ in range(6):
        assert run.fail(("Bash", {"command": command})) is None
    assert run.pre(EDIT) is None


@pytest.mark.parametrize(
    ("command", "error"),
    [
        ("grep -rn checkout src", "Exit code 2\ngrep: src: No such file or directory"),
        ("grep -rn checkout src", "Command timed out"),
        ("grep -rn checkout src", "Error\nExit code 1"),
        ("cd src; grep -rn checkout .", "Exit code 1"),
        ("cd src || grep -rn checkout .", "Exit code 1"),
        ("(cd src && grep -rn checkout .)", "Exit code 1"),
        ("cd src && grep -rn checkout . && npm test", "Exit code 1"),
        ("cd src && npm test", "Exit code 1"),
        ("npm ci && grep -rn checkout src", "Exit code 1"),
        ("./test unit", "Exit code 1"),
        ("bin/test unit", "Exit code 1"),
        ("cd /work/shop && ./test unit", "Exit code 1"),
        ("/work/shop/[ x ]", "Exit code 1"),
        ("grep -rn checkout src || npm test", "Exit code 1"),
        ("grep -rn checkout src; npm test", "Exit code 1"),
        ("grep -rn checkout src | sort", "Exit code 1"),
        ("(grep -rn checkout src)", "Exit code 1"),
        ("find src -name '*.py' | xargs grep checkout", "Exit code 1"),
        ("npm test", "Exit code 1"),
        ("git diff", "Exit code 1"),
        ("git log --grep checkout", "Exit code 1"),
        ("echo $(grep x", "Exit code 1"),
    ],
)
def test_other_failures_still_count(tmp_path, command, error):
    run = Run(tmp_path)
    for _ in range(5):
        run.fail(("Bash", {"command": command}), error=error)
    reason = run.pre(EDIT)
    assert reason
    assert "rule failure_streak" in reason


def test_a_failed_search_tool_still_counts_and_a_no_match_does_not_reset(tmp_path):
    grep_tool: Call = ("Grep", {"pattern": "checkout", "path": "/work/shop/src"})
    run = Run(tmp_path)
    for _ in range(5):
        run.fail(grep_tool)
    assert run.pre(EDIT)
    for _ in range(4):
        run.fail()
    run.fail(("Bash", {"command": "grep -rn checkout src"}))
    run.fail()
    reason = run.pre(EDIT)
    assert reason
    assert "5 calls failed in a row" in reason


def test_failure_streak_wins_over_a_loop(tmp_path):
    run = Run(tmp_path)
    for _ in range(2):
        run.pre(TEST)
        run.fail(TEST)
    for i in range(3):
        run.fail(("Bash", {"command": f"step {i}"}))
    reason = run.pre(TEST)
    assert reason
    assert "rule failure_streak" in reason


def test_each_deny_and_reminder_is_logged_without_the_tool_input(tmp_path):
    run = Run(tmp_path)
    assert denied(run, [TEST, TEST, TEST, LINT]) == [2]
    for i in range(5):
        run.fail(("Bash", {"command": f"step {i}"}))
    assert run.pre(EDIT)
    run.clock += 600
    assert run.post(READ)
    assert run.post(READ) is None
    events = guard_events(tmp_path)
    fields = ["time", "hook", "session", "agent_id", "agent_type", "tool", "rule", "decision"]
    assert [list(event) for event in events] == [fields] * 3
    same = {"hook": "guard_runaway", "session": "session-1", **SUBAGENT}
    assert events == [
        {"time": "1970-01-12T13:46:40+00:00", **same, "tool": "Bash", "rule": "loop"}
        | {"decision": "deny"},
        {"time": "1970-01-12T13:46:40+00:00", **same, "tool": "Edit", "rule": "failure_streak"}
        | {"decision": "deny"},
        {"time": "1970-01-12T13:56:40+00:00", **same, "tool": "Read", "rule": "long_run"}
        | {"decision": "context"},
    ]
    text = (tmp_path / "guard-events.jsonl").read_text(encoding="utf-8")
    assert "npm" not in text
    assert "cart" not in text
    assert "step" not in text


def test_allowed_calls_log_nothing(tmp_path):
    run = Run(tmp_path)
    assert denied(run, [TEST, TEST, READ, EDIT]) == []
    run.fail()
    run.post()
    assert guard_events(tmp_path) == []


def test_a_failed_log_write_keeps_the_decision(tmp_path, capsys):
    (tmp_path / "guard-events.jsonl").mkdir()
    run = Run(tmp_path)
    assert denied(run, [TEST, TEST, TEST]) == [2]
    run.clock += 600
    assert run.post()
    assert capsys.readouterr().err.count("guard_runaway could not log the guard event") == 2


def test_long_run_warns_once_after_the_threshold(tmp_path):
    run = Run(tmp_path)
    assert run.pre(EDIT) is None
    run.clock += 599
    assert run.post() is None
    run.clock += 1
    assert run.post() == (
        "agent-harness runaway guard: you have run 10 minutes. If what is left will take more "
        "than about 10 more minutes (long tests, backtests, waiting for a pipeline), stop and "
        "report what is done, what is left, the expected time and the options, so the lead "
        "can decide."
    )
    run.clock += DAY
    assert run.post() is None
    assert run.fail() is None
    assert run.pre(LINT) is None


def test_long_run_warns_after_a_failure_too_and_never_before_a_call(tmp_path):
    run = Run(tmp_path)
    run.post()
    run.clock += 3600
    assert run.pre(EDIT) is None
    context = run.fail()
    assert context
    assert context.startswith("agent-harness runaway guard: you have run 60 minutes.")
    assert run.post() is None


def test_long_run_counts_from_each_agents_first_call(tmp_path):
    first = Run(tmp_path)
    second = Run(tmp_path, {"agent_id": "agent-2", "agent_type": "Explore"})
    first.pre(READ)
    first.clock = second.clock = START + 300
    second.pre(READ)
    first.clock = second.clock = START + 600
    assert first.post(READ)
    assert second.post(READ) is None
    first.clock = second.clock = START + 900
    assert second.post(READ)


@pytest.mark.parametrize(
    ("runaway", "after", "expected"),
    [
        pytest.param({"warn_minutes": 0}, 30 * DAY, None, id="zero-disables"),
        pytest.param({"warn_minutes": 2.5}, 149, None, id="fraction-before"),
        pytest.param({"warn_minutes": 2.5}, 150, 2, id="fraction-after"),
        pytest.param({"warn_minutes": 30}, 1799, None, id="longer-before"),
        pytest.param({"warn_minutes": 30}, 1800, 30, id="longer-after"),
    ],
)
def test_long_run_settings(tmp_path, runaway, after, expected):
    write_json(tmp_path / "guard.json", {"runaway": runaway})
    run = Run(tmp_path)
    run.pre(EDIT)
    run.clock += after
    context = run.post()
    if expected is None:
        assert context is None
        return
    assert context
    assert context.startswith(f"agent-harness runaway guard: you have run {expected} minutes.")


def test_project_settings_override_the_users(tmp_path):
    write_json(tmp_path / "guard.json", {"runaway": {"loop_repeats": 2, "failure_streak": 2}})
    assert denied(Run(tmp_path / "user-only"), [TEST, TEST, TEST]) == [2]
    assert denied(Run(tmp_path), [TEST, TEST, TEST]) == [1, 2]
    project = tmp_path / "shop"
    project_settings = {"guard": {"runaway": {"loop_repeats": 4}}}
    write_json(project / ".claude" / "agent-harness.json", project_settings)
    run = Run(tmp_path, cwd=str(project), session="project-session")
    assert denied(run, [LINT] * 4) == [3]
    assert streak_denials(run, "ffp") == [2]
    elsewhere = Run(tmp_path, cwd="/work/other", session="env", CLAUDE_PROJECT_DIR=str(project))
    assert denied(elsewhere, [LINT] * 4) == [3]


def test_loop_repeats_zero_turns_off_only_the_loop(tmp_path):
    write_json(tmp_path / "guard.json", {"runaway": {"loop_repeats": 0}})
    run = Run(tmp_path)
    assert denied(run, [TEST] * 6) == []
    assert streak_denials(run, "fffffp") == [5]


@pytest.mark.parametrize(
    ("excluded", "agent_type", "off"),
    [
        (["ci-watcher"], "agent-harness:ci-watcher", True),
        (["ci-watcher"], "ci-watcher", True),
        (["agent-harness:ci-watcher"], "agent-harness:ci-watcher", True),
        (["agent-harness:ci-watcher"], "other:ci-watcher", False),
        (["ci-watcher"], "agent-harness:implementer", False),
        (["ci-watcher"], "ci-watcher-2", False),
        ([], "ci-watcher", False),
    ],
)
def test_excluded_agents_are_never_touched(tmp_path, excluded, agent_type, off):
    write_json(tmp_path / "guard.json", {"runaway": {"exclude_agents": excluded}})
    run = Run(tmp_path, {"agent_id": "agent-1", "agent_type": agent_type})
    loops = denied(run, [TEST] * 3)
    for i in range(5):
        run.fail(("Bash", {"command": f"step {i}"}))
    streak = run.pre(EDIT)
    run.clock += DAY
    note = run.post()
    if off:
        assert (loops, streak, note) == ([], None, None)
        assert not (tmp_path / "runaway").exists()
        assert guard_events(tmp_path) == []
        return
    assert loops == [2]
    assert streak
    assert note


def test_excluded_agents_add_up_across_the_config_files(tmp_path):
    write_json(tmp_path / "guard.json", {"runaway": {"exclude_agents": ["ci-watcher"]}})
    project = tmp_path / "shop"
    write_json(
        project / ".claude" / "agent-harness.json",
        {"guard": {"runaway": {"exclude_agents": ["deploy-watcher"]}}},
    )
    for agent in ("ci-watcher", "deploy-watcher"):
        run = Run(tmp_path, {"agent_id": agent, "agent_type": agent}, cwd=str(project))
        assert denied(run, [TEST] * 3) == []
    run = Run(tmp_path, {"agent_id": "a-3", "agent_type": "implementer"}, cwd=str(project))
    assert denied(run, [TEST] * 3) == [2]


def test_all_rules_off_writes_no_state(tmp_path):
    off = {"loop_repeats": 0, "failure_streak": 0, "warn_minutes": 0}
    write_json(tmp_path / "guard.json", {"runaway": off})
    run = Run(tmp_path)
    assert denied(run, [TEST] * 4) == []
    assert run.fail() is None
    assert not (tmp_path / "runaway").exists()


@pytest.mark.parametrize(
    ("raw", "warning"),
    [
        ('{"loop_repeats": 1}', "runaway.loop_repeats must be 0 or at least 2, so it is ignored"),
        ('{"loop_repeats": "2"}', "runaway.loop_repeats must be a whole number, 0 or more"),
        ('{"loop_repeats": 2.0}', "runaway.loop_repeats must be a whole number, 0 or more"),
        ('{"loop_repeats": -2}', "runaway.loop_repeats must be a whole number, 0 or more"),
        ('{"loop_repeats": true}', "runaway.loop_repeats must be a whole number, 0 or more"),
        ('{"loop_repeats": null}', "runaway.loop_repeats must be a whole number, 0 or more"),
        ('{"failure_streak": 1.5}', "runaway.failure_streak must be a whole number, 0 or more"),
        ('{"warn_minutes": NaN}', "runaway.warn_minutes must be a number, 0 or more"),
        ('{"warn_minutes": Infinity}', "runaway.warn_minutes must be a number, 0 or more"),
        ('{"warn_minutes": 1' + "0" * 400 + "}", "runaway.warn_minutes must be a number"),
        ('{"warn_minutes": -1}', "runaway.warn_minutes must be a number, 0 or more"),
        ('{"warn_minutes": "10"}', "runaway.warn_minutes must be a number, 0 or more"),
        ('{"warn_minutes": false}', "runaway.warn_minutes must be a number, 0 or more"),
        ('{"loops": 2}', "unknown key runaway.loops"),
        ('{"exclude_agents": "ci-watcher"}', "runaway.exclude_agents must be a list of strings"),
        ('{"exclude_agents": [1]}', "runaway.exclude_agents must be a list of strings"),
        ("[3]", "runaway is not a JSON object, so it is ignored"),
    ],
)
def test_invalid_settings_keep_the_defaults_and_are_named(tmp_path, raw, warning):
    (tmp_path / "guard.json").write_text(f'{{"runaway": {raw}}}', encoding="utf-8")
    run = Run(tmp_path)
    reasons = [run.pre(TEST) for _ in range(3)]
    assert reasons[:2] == [None, None]
    assert reasons[2]
    assert f"Runaway guard config warnings: {tmp_path / 'guard.json'}: " in reasons[2]
    assert warning in reasons[2]
    event = {"cwd": "/work/shop"}
    settings = guard_runaway.load_settings(event, run.environ)
    defaults = (settings.loop_repeats, settings.failure_streak, settings.warn_minutes)
    assert defaults == (3, 5, 10)


def test_an_unreadable_guard_file_is_named_and_the_defaults_apply(tmp_path):
    (tmp_path / "guard.json").write_text("{runaway", encoding="utf-8")
    reason = denied_reason(Run(tmp_path))
    assert "is not valid JSON" in reason


def denied_reason(run: Run) -> str:
    reasons = [run.pre(TEST) for _ in range(3)]
    assert reasons[:2] == [None, None]
    assert reasons[2]
    return reasons[2]


@pytest.mark.parametrize(
    "content",
    [
        "{not json",
        "",
        "[]",
        '{"warned": true}',
        '{"started": "soon"}',
        '{"started": true}',
        '{"started": NaN}',
        '{"started": 1e999}',
        '{"started": 1' + "0" * 400 + "}",
    ],
)
def test_a_corrupt_state_file_starts_over(tmp_path, content):
    run = Run(tmp_path)
    run.state_file().parent.mkdir(parents=True)
    run.state_file().write_text(content, encoding="utf-8")
    assert denied(run, [TEST, TEST, TEST]) == [2]
    state = json.loads(run.state_file().read_text(encoding="utf-8"))
    assert state["started"] == START
    assert state["warned"] is False


def test_bad_fields_in_a_state_file_fall_back_field_by_field(tmp_path):
    path = tmp_path / "state.json"
    write_json(path, {"started": 5, "warned": "yes", "streak": -3, "runs": {"a": "2", "b": 2}})
    state = guard_runaway.read_state(path)
    assert state == guard_runaway.State(5.0, False, 0, {"b": 2})
    write_json(path, {"started": 5.5, "warned": True, "streak": True, "runs": [1]})
    assert guard_runaway.read_state(path) == guard_runaway.State(5.5, True, 0, {})


def test_state_holds_hashes_and_leaves_no_temp_files(tmp_path):
    run = Run(tmp_path)
    run.pre(TEST)
    run.fail(TEST)
    run.pre(READ)
    run.post(READ)
    names = sorted(path.name for path in run.state_file().parent.iterdir())
    assert names == [".lock", "agent-1.json"]
    text = run.state_file().read_text(encoding="utf-8")
    assert "npm" not in text
    assert "cart" not in text


def test_session_and_agent_ids_cannot_leave_the_state_dir(tmp_path):
    run = Run(tmp_path / "home", {"agent_id": "../../../escape"}, session="../../out")
    assert denied(run, [TEST] * 3) == [2]
    log = tmp_path / "home" / "guard-events.jsonl"
    files = [path for path in tmp_path.rglob("*") if path.is_file() and path != log]
    root = tmp_path / "home" / "runaway"
    assert files
    assert all(path.parent.parent == root for path in files)


def test_old_sessions_are_removed_when_an_agent_starts(tmp_path):
    root = tmp_path / "runaway"
    now = time.time()
    sessions = {"old": now - 3 * DAY, "recent": now - DAY, "session-1": now - 5 * DAY}
    for name, mtime in sessions.items():
        write_json(root / name / "agent-9.json", {"started": mtime})
        (root / name / ".lock").touch()
        os.utime(root / name, (mtime, mtime))
    (root / "stray-file").write_text("x", encoding="utf-8")
    os.utime(root / "stray-file", (now - 9 * DAY, now - 9 * DAY))
    run = Run(tmp_path)
    run.clock = now
    run.pre(TEST)
    assert sorted(path.name for path in root.iterdir()) == ["recent", "session-1", "stray-file"]
    assert (root / "session-1" / "agent-9.json").is_file()


def test_old_sessions_stay_while_an_agent_keeps_working(tmp_path):
    run = Run(tmp_path)
    run.pre(TEST)
    old = tmp_path / "runaway" / "old"
    write_json(old / "agent-9.json", {"started": 0})
    os.utime(old, (0, 0))
    run.clock = time.time()
    run.pre(LINT)
    assert old.is_dir()


def test_main_allows_after_an_internal_error(tmp_path, capsys):
    home = tmp_path / "home"
    home.write_text("not a directory", encoding="utf-8")
    event: dict[str, Any] = {"session_id": "s", "hook_event_name": "PreToolUse"}
    event["tool_name"] = "Bash"
    event |= {"tool_input": {"command": "npm test"}, **SUBAGENT}
    stdout = StringIO()
    status = guard_runaway.main(StringIO(json.dumps(event)), stdout, {"HARNESS_HOME": str(home)})
    assert status == 0
    assert stdout.getvalue() == ""
    assert "guard_runaway.py allowed the call after an internal error" in capsys.readouterr().err


@pytest.mark.parametrize("stdin", ["", "not json", "[]", '"text"'])
def test_main_ignores_input_that_is_not_an_event(tmp_path, stdin):
    stdout = StringIO()
    assert guard_runaway.main(StringIO(stdin), stdout, {"HARNESS_HOME": str(tmp_path)}) == 0
    assert stdout.getvalue() == ""


def test_the_script_imports_its_siblings_and_denies_the_third_repeat(tmp_path):
    event: dict[str, Any] = {"session_id": "s", "cwd": str(tmp_path)}
    event["hook_event_name"] = "PreToolUse"
    event |= {"tool_name": "Bash", "tool_input": {"command": "npm test"}, **SUBAGENT}
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path), "HARNESS_HOME": str(tmp_path)}
    results = [
        subprocess.run(  # noqa: S603
            [sys.executable, str(SCRIPT)],
            input=json.dumps(event),
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        for _ in range(3)
    ]
    assert [result.returncode for result in results] == [0, 0, 0]
    assert [result.stdout for result in results[:2]] == ["", ""]
    output = json.loads(results[2].stdout)["hookSpecificOutput"]
    assert output["permissionDecision"] == "deny"
    assert "rule loop" in output["permissionDecisionReason"]


SUBAGENT_ONLY = {"guard_shell", "save_artifacts", "hashlib", "shutil", "tempfile", "datetime"}
SUBAGENT_ONLY |= {"pathlib"}


def test_a_main_session_call_imports_only_what_it_needs(tmp_path):
    event: dict[str, Any] = {"session_id": "s", "hook_event_name": "PreToolUse"}
    event |= {"tool_name": "Bash", "tool_input": {"command": "npm test"}, "cwd": str(tmp_path)}
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path), "HARNESS_HOME": str(tmp_path)}
    imported = []
    for identity in ({}, SUBAGENT):
        result = subprocess.run(  # noqa: S603
            [sys.executable, "-X", "importtime", str(SCRIPT)],
            input=json.dumps(event | identity),
            capture_output=True,
            text=True,
            env=env,
            check=True,
        )
        names = {line.rsplit("|", 1)[-1].strip() for line in result.stderr.splitlines()}
        imported.append(names & SUBAGENT_ONLY)
    assert imported == [set(), SUBAGENT_ONLY]


def test_hooks_json_runs_the_guard_on_every_tool_call():
    config = json.loads((HOOKS / "hooks.json").read_text(encoding="utf-8"))
    for name in ("PreToolUse", "PostToolUse", "PostToolUseFailure"):
        groups = [
            group
            for group in config["hooks"][name]
            if any("guard_runaway.py" in handler["command"] for handler in group["hooks"])
        ]
        assert len(groups) == 1, name
        assert groups[0].get("matcher", "*") in ("", "*"), name
