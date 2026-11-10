import importlib
import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

HOOKS = Path(__file__).resolve().parents[1] / "hooks"
AGENT = "agent-harness:implementer"
OWN = "agent-harness-implementer"
NOW = datetime.now(UTC)


class Clock:
    def __init__(self, now: float) -> None:
        self.now = now

    def time(self) -> float:
        return self.now


@pytest.fixture
def cap(monkeypatch, tmp_path) -> ModuleType:
    monkeypatch.syspath_prepend(str(HOOKS))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    module = importlib.import_module("memory_cap")
    monkeypatch.setattr(module, "time", Clock((NOW - timedelta(minutes=6)).timestamp()))
    return module


def begin(cap: ModuleType, tmp_path: Path, minutes_ago: float = 6, agent_id: str = "a1") -> None:
    cap.time.now = (NOW - timedelta(minutes=minutes_ago)).timestamp()
    assert cap.decision(started(agent_id), env(tmp_path)) is None


def memory(tmp_path: Path, agent: str = OWN) -> Path:
    directory = tmp_path / "home" / ".claude" / "agent-memory" / agent
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def grow(path: Path, kb: float) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x" * int(kb * 1024))
    return path


def stamp(moment: datetime) -> str:
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def bash(command: str) -> tuple[str, dict[str, Any]]:
    return "Bash", {"command": command}


def appended(name: str = "commands.md", directory: str = OWN) -> tuple[str, dict[str, Any]]:
    return bash(f"cat notes >> ~/.claude/agent-memory/{directory}/{name}")


def edit(path: Path) -> tuple[str, dict[str, Any]]:
    return "Edit", {"file_path": str(path), "old_string": "x", "new_string": "y"}


def prompt(
    text: str, minutes_ago: float, prompt_id: str | None = None, **fields: Any
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "type": "user",
        "timestamp": stamp(NOW - timedelta(minutes=minutes_ago)),
    }
    entry |= {"message": {"content": text}, **fields}
    return entry | ({"promptId": prompt_id} if prompt_id else {})


def calls(*made: tuple[str, dict[str, Any]], minutes_ago: float = 0.5) -> list[dict[str, Any]]:
    entries = []
    for number, (tool, tool_input) in enumerate(made):
        call = f"c{minutes_ago}-{number}"
        use = {"type": "tool_use", "id": call, "name": tool, "input": tool_input}
        result = {"type": "tool_result", "tool_use_id": call, "content": "ok"}
        moment = stamp(NOW - timedelta(minutes=minutes_ago))
        entries.append({"type": "assistant", "timestamp": moment, "message": {"content": [use]}})
        entries.append({"type": "user", "timestamp": moment, "message": {"content": [result]}})
    return entries


def transcript(tmp_path: Path, entries: list[dict[str, Any]], agent_id: str = "a1") -> Path:
    path = tmp_path / "session" / "subagents" / f"agent-{agent_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(entry) + "\n" for entry in entries))
    return path


def final(minutes_ago: float = 3) -> dict[str, Any]:
    moment = stamp(NOW - timedelta(minutes=minutes_ago))
    content = [{"type": "text", "text": "Report."}]
    return {"type": "assistant", "timestamp": moment, "message": {"content": content}}


def task(
    *made: tuple[str, dict[str, Any]], text: str = "SHOP-412: implement it"
) -> list[dict[str, Any]]:
    return [prompt(text, 5, "p1"), *calls(*made, minutes_ago=4)]


def started(agent_id: str = "a1") -> dict[str, Any]:
    return {
        "hook_event_name": "SubagentStart",
        "session_id": "s1",
        "agent_id": agent_id,
        "agent_type": AGENT,
    }


def stop(
    tmp_path: Path, active: bool = False, agent: Any = AGENT, agent_id: str = "a1"
) -> dict[str, Any]:
    path = tmp_path / "session" / "subagents" / f"agent-{agent_id}.jsonl"
    return {
        "hook_event_name": "SubagentStop",
        "session_id": "s1",
        "agent_id": agent_id,
        "agent_type": agent,
        "stop_hook_active": active,
        "agent_transcript_path": str(path),
    }


def handback(tmp_path: Path, tool: str = "SubagentHandback", agent: Any = AGENT) -> dict[str, Any]:
    return {
        "hook_event_name": "PreToolUse",
        "tool_name": tool,
        "tool_input": {"message": "Done."},
        "session_id": "s1",
        "agent_id": "a1",
        "agent_type": agent,
        "transcript_path": str(tmp_path / "session.jsonl"),
    }


def env(tmp_path: Path) -> dict[str, str]:
    return {"HARNESS_HOME": str(tmp_path / "harness")}


def state(tmp_path: Path, agent_id: str = "a1") -> Path:
    return tmp_path / "harness" / "memory-cap" / f"s1-{agent_id}.json"


def kind(verdict: dict[str, Any] | None) -> str:
    if verdict is None:
        return "pass"
    if verdict.get("decision") == "block":
        return "block"
    if "hookSpecificOutput" in verdict:
        return "deny"
    assert set(verdict) == {"systemMessage"}
    return "warn"


@pytest.mark.parametrize(
    ("name", "kb", "shown"),
    [
        ("commands.md", 9, "9.0 KB, over its 8 KB cap"),
        ("MEMORY.md", 5, "5.0 KB, over its 4 KB cap"),
        ("topics/commands.md", 9, "9.0 KB, over its 8 KB cap"),
    ],
)
def test_growth_named_through_bash_blocks_the_stop_once(tmp_path, cap, name, kb, shown):
    path = grow(memory(tmp_path) / name, 1)
    begin(cap, tmp_path)
    grow(path, kb)
    transcript(tmp_path, task(appended(name)))

    first = cap.decision(stop(tmp_path), env(tmp_path))
    later = [kind(cap.decision(stop(tmp_path, active=True), env(tmp_path))) for _ in range(2)]

    assert kind(first) == "block"
    assert first is not None
    assert f"- {path}: {shown}, written through Bash, which skips the size check" in first["reason"]
    assert "over its size cap after this run's changes" in first["reason"]
    assert "topic file linked from MEMORY.md rather than deleting lessons" in first["reason"]
    assert "only with Edit or Write" in first["reason"]
    assert "reply with the whole report again" in first["reason"]
    assert later == ["warn", "warn"]


def test_growth_named_by_a_file_tool_does_not_blame_bash(tmp_path, cap):
    path = grow(memory(tmp_path) / "commands.md", 1)
    begin(cap, tmp_path)
    grow(path, 9)
    transcript(tmp_path, task(edit(path)))

    verdict = cap.decision(stop(tmp_path), env(tmp_path))

    assert kind(verdict) == "block"
    assert verdict is not None
    assert f"- {path}: 9.0 KB, over its 8 KB cap\n" in verdict["reason"]
    assert "Bash" not in verdict["reason"]


@pytest.mark.parametrize("recorded", [True, False])
@pytest.mark.parametrize("ending", ["text", "handback"])
@pytest.mark.parametrize(
    "follow_up",
    [
        {"prompt_id": "p2"},
        {"prompt_id": "p2", "isMeta": True, "origin": {"kind": "coordinator"}},
        {},
    ],
)
def test_a_resume_that_writes_nothing_does_not_block(tmp_path, cap, recorded, ending, follow_up):
    path = grow(memory(tmp_path) / "commands.md", 1)
    if recorded:
        begin(cap, tmp_path)
    grow(path, 9)
    report = calls(("SubagentHandback", {"message": "Report."}), minutes_ago=3)[:1]
    first = [*task(appended()), *(report if ending == "handback" else [final()])]
    transcript(tmp_path, first)
    assert kind(cap.decision(stop(tmp_path), env(tmp_path))) == "block"
    assert kind(cap.decision(stop(tmp_path, active=True), env(tmp_path))) == "warn"
    if recorded:
        begin(cap, tmp_path, minutes_ago=1.5)
    resumed = [*first, prompt("Round 2: fix the review findings", 1, **follow_up)]
    transcript(tmp_path, [*resumed, *calls(bash("uv run pytest -q"))])

    assert kind(cap.decision(stop(tmp_path), env(tmp_path))) == "pass"
    assert kind(cap.decision(handback(tmp_path), env(tmp_path))) == "pass"


@pytest.mark.parametrize("recorded", [True, False])
def test_a_resume_that_grows_the_file_again_blocks_again(tmp_path, cap, recorded):
    path = grow(memory(tmp_path) / "commands.md", 1)
    if recorded:
        begin(cap, tmp_path)
    grow(path, 9)
    first = [*task(appended()), final()]
    transcript(tmp_path, first)
    cap.decision(stop(tmp_path), env(tmp_path))
    cap.decision(stop(tmp_path, active=True), env(tmp_path))
    if recorded:
        begin(cap, tmp_path, minutes_ago=1.5)
    grow(path, 12)
    follow_up = prompt("Round 2", 1, "p2", isMeta=True, origin={"kind": "coordinator"})
    transcript(tmp_path, [*first, follow_up, *calls(appended())])

    assert kind(cap.decision(handback(tmp_path), env(tmp_path))) == "deny"


@pytest.mark.parametrize("recorded", [True, False])
def test_a_write_before_the_resume_does_not_name_a_file_another_run_grows(tmp_path, cap, recorded):
    path = grow(memory(tmp_path) / "commands.md", 7)
    if recorded:
        begin(cap, tmp_path)
    first = [*task(appended()), final()]
    if recorded:
        begin(cap, tmp_path, minutes_ago=1.5)
    replay = {"type": "api-request-blob", "message": {"content": first[1]["message"]["content"]}}
    follow_up = prompt("Round 2", 1, "p2", isMeta=True, origin={"kind": "coordinator"})
    transcript(tmp_path, [*first, follow_up, replay, *calls(bash("uv run pytest -q"))])
    grow(path, 9)

    assert kind(cap.decision(stop(tmp_path), env(tmp_path))) == "pass"


def test_with_a_start_record_the_run_counts_from_the_start_not_the_last_prompt(tmp_path, cap):
    path = grow(memory(tmp_path) / "commands.md", 1)
    begin(cap, tmp_path)
    grow(path, 9)
    follow_up = prompt("Round 2", 2, "p2", isMeta=True, origin={"kind": "coordinator"})
    transcript(tmp_path, [*task(appended()), final(), follow_up])

    assert kind(cap.decision(stop(tmp_path), env(tmp_path))) == "block"


MID_TASK = [
    {"prompt_id": "p2", "isMeta": True, "origin": {"kind": "coordinator"}},
    {"prompt_id": "p3", "isMeta": True, "origin": {"kind": "task-notification"}},
    {"prompt_id": "p4", "isMeta": True},
]


@pytest.mark.parametrize("recorded", [True, False])
@pytest.mark.parametrize("boundary", MID_TASK)
def test_a_message_between_the_write_and_the_stop_does_not_hide_the_write(
    tmp_path, cap, recorded, boundary
):
    path = grow(memory(tmp_path) / "commands.md", 1)
    if recorded:
        begin(cap, tmp_path)
    grow(path, 9)
    entries = [
        *task(appended(), bash("uv run pytest -q")),
        prompt("A message while you work", 2, **boundary),
        *calls(bash("git diff"), minutes_ago=1),
    ]
    if boundary.get("prompt_id") == "p4":
        entries = [
            *task(appended()),
            final(),
            prompt("[handback-send-enforce] Call it", 2, **boundary),
        ]
    transcript(tmp_path, entries)

    assert kind(cap.decision(stop(tmp_path), env(tmp_path))) == "block"
    entries += [
        prompt("Stop hook feedback: the cap", 0.8, "p1", isMeta=True),
        prompt(
            "Another message while you work", 0.7, "p5", isMeta=True, origin={"kind": "coordinator"}
        ),
        *calls(appended(), minutes_ago=0.6),
    ]
    transcript(tmp_path, entries)
    assert kind(cap.decision(stop(tmp_path, active=True), env(tmp_path))) == "warn"


def test_an_overlapping_run_that_never_named_the_file_does_not_block(tmp_path, cap):
    path = grow(memory(tmp_path) / "commands.md", 1)
    for agent_id in ("A", "B"):
        begin(cap, tmp_path, agent_id=agent_id)
    grow(path, 9)
    transcript(tmp_path, task(appended(directory="agent-harness-reviewer-qa")), agent_id="A")
    transcript(tmp_path, task(appended()), agent_id="B")

    assert kind(cap.decision(stop(tmp_path, agent_id="A"), env(tmp_path))) == "pass"
    assert kind(cap.decision(stop(tmp_path, agent_id="B"), env(tmp_path))) == "block"


DIR = f"~/.claude/agent-memory/{OWN}"
WRITES = [
    f"cat >> {DIR}/commands.md <<'EOF'\n- lesson\nEOF",
    f"cd {DIR} && echo '- lesson' >> commands.md && tail -3 commands.md",
    f'printf x > "{DIR}/commands.md"',
    f"cd {DIR} && cat new.md 2>/dev/null >| commands.md",
    f"sed -i '' 's/a/b/' {DIR}/commands.md",
    f"LC_ALL=C sed -i '' 's/a/b/' {DIR}/commands.md",
    f"cd {DIR} && sed -E -i.bak -e 's/a/b/' commands.md",
    f"perl -pi -e 's/a/b/' {DIR}/commands.md",
    f"echo '- lesson' | tee -a {DIR}/commands.md",
    f"cp /tmp/draft.md {DIR}/commands.md",
    f"mv /tmp/commands.md {DIR}/",
    f"rsync -a /tmp/commands.md {DIR}/",
    f"install -m 644 /tmp/commands.md {DIR}/commands.md",
    f"python3 - <<'EOF'\np = '{DIR}/commands.md'\nopen(p, 'a').write('x')\nEOF",
    f"cd {DIR} && python3 - <<'EOF'\nf = 'commands.md'\n"
    "with open(f, 'w', encoding='utf-8') as h:\n    h.write(s)\nEOF",
    f"python3 -c \"from pathlib import Path; Path('{DIR}/commands.md').write_text('x')\"",
    f"python3 - <<'EOF'\nfrom pathlib import Path\n"
    f"Path('{DIR}/commands.md').write_bytes(b'x')\nEOF",
    f"python3 - <<'EOF'\nfrom pathlib import Path\n"
    f"with Path('{DIR}/commands.md').open(mode='x') as h: pass\nEOF",
]
READS = [
    f"cat {DIR}/commands.md",
    f"head -5 {DIR}/commands.md && grep -n lesson {DIR}/commands.md",
    f"cd {DIR} && wc -c MEMORY.md commands.md",
    f"sed -n 1,40p {DIR}/commands.md",
    f"cp {DIR}/commands.md /tmp/backup.md",
    f"cat {DIR}/commands.md && sed -i '' 's/a/b/' src/app.py",
    f"diff {DIR}/commands.md /tmp/old.md > /tmp/out.txt 2>&1",
    f"python3 -c \"print(open('{DIR}/commands.md').read())\"",
    "cat >> ~/.claude/agent-memory/agent-harness-reviewer-qa/commands.md",
    "echo x >> commands.md",
]


@pytest.mark.parametrize("command", WRITES)
def test_each_write_shape_names_the_file(tmp_path, cap, command):
    path = memory(tmp_path) / "commands.md"

    assert cap.bash_writes(command.replace("~", str(tmp_path / "home")), path, OWN)
    assert cap.bash_writes(command, path, OWN)


@pytest.mark.parametrize("command", READS)
def test_a_read_or_another_target_does_not_name_the_file(tmp_path, cap, command):
    assert not cap.bash_writes(command, memory(tmp_path) / "commands.md", OWN)


def test_a_parallel_run_that_only_read_the_file_is_not_blocked(tmp_path, cap):
    path = grow(memory(tmp_path) / "commands.md", 7)
    for agent_id in ("A", "B"):
        begin(cap, tmp_path, agent_id=agent_id)
    transcript(tmp_path, task(bash(f"cat {path}")), agent_id="A")
    transcript(tmp_path, task(bash(f"cat >> {path} <<'EOF'\n- lesson\nEOF")), agent_id="B")
    grow(path, 9)

    assert kind(cap.decision(stop(tmp_path, agent_id="A"), env(tmp_path))) == "pass"
    assert kind(cap.decision(stop(tmp_path, agent_id="B"), env(tmp_path))) == "block"


def test_a_shrink_of_a_file_already_over_the_cap_does_not_block(tmp_path, cap):
    path = grow(memory(tmp_path) / "commands.md", 10)
    begin(cap, tmp_path)
    grow(path, 9)
    transcript(tmp_path, task(edit(path)))

    assert kind(cap.decision(stop(tmp_path), env(tmp_path))) == "pass"
    assert kind(cap.decision(handback(tmp_path), env(tmp_path))) == "pass"


def test_a_new_file_over_the_cap_blocks(tmp_path, cap):
    memory(tmp_path)
    begin(cap, tmp_path)
    grow(memory(tmp_path) / "dump.md", 9)
    transcript(tmp_path, task(bash(f"cp big.md ~/.claude/agent-memory/{OWN}/dump.md")))

    assert kind(cap.decision(stop(tmp_path), env(tmp_path))) == "block"


@pytest.mark.parametrize(
    "leftover",
    [
        {"blocks": 1},
        {"blocks": 1, "denied_at_handback": True},
        {"blocks": 1, "segment": "0:2026-01-01T10:00:00.000Z", "delivered": True},
    ],
)
@pytest.mark.parametrize("start_fires", [True, False])
def test_leftover_state_does_not_soften_the_next_run(tmp_path, cap, leftover, start_fires):
    path = grow(memory(tmp_path) / "commands.md", 1)
    state(tmp_path).parent.mkdir(parents=True)
    state(tmp_path).write_text(json.dumps(leftover))
    if start_fires:
        begin(cap, tmp_path)
    grow(path, 9)
    transcript(tmp_path, task(appended()))

    assert kind(cap.decision(handback(tmp_path), env(tmp_path))) == "deny"


def test_the_start_records_the_sizes_and_replaces_older_state(tmp_path, cap):
    grow(memory(tmp_path) / "MEMORY.md", 1)
    grow(memory(tmp_path) / "topics" / "commands.md", 2)
    grow(memory(tmp_path, "agent-harness-reviewer-qa") / "checks.md", 3)
    folder = state(tmp_path).parent
    folder.mkdir(parents=True)
    state(tmp_path).write_text(json.dumps({"blocks": 1, "segment": "3:x", "delivered": True}))
    old, recent = folder / "s0-old.json", folder / "s0-recent.json"
    for path in (old, recent):
        path.write_text("{}")
    three_days_ago = time.time() - 3 * 24 * 3600
    os.utime(old, (three_days_ago, three_days_ago))

    assert cap.decision({**started(), "agent_id": "agent-a1"}, env(tmp_path)) is None
    cap.decision({**started(), "agent_id": "x1", "agent_type": "Explore"}, env(tmp_path))

    assert json.loads(state(tmp_path).read_text()) == {
        "baseline": {"MEMORY.md": 1024, "topics/commands.md": 2048},
        "started": cap.time.now,
    }
    assert (old.exists(), recent.exists()) == (False, True)
    assert not state(tmp_path, "x1").exists()


def test_without_a_recorded_start_the_segment_start_decides(tmp_path, cap):
    grown = grow(memory(tmp_path) / "commands.md", 9)
    older = grow(memory(tmp_path) / "traps.md", 9)
    before = (NOW - timedelta(minutes=10)).timestamp()
    os.utime(older, (before, before))
    transcript(tmp_path, task(appended(), appended("traps.md")))

    verdict = cap.decision(stop(tmp_path), env(tmp_path))

    assert kind(verdict) == "block"
    assert verdict is not None
    assert str(grown) in verdict["reason"]
    assert str(older) not in verdict["reason"]


def test_a_handback_is_denied_once_and_the_stop_after_it_does_not_block(tmp_path, cap):
    path = grow(memory(tmp_path) / "commands.md", 1)
    begin(cap, tmp_path)
    grow(path, 9)
    transcript(tmp_path, task(appended()))

    first = cap.decision(handback(tmp_path), env(tmp_path))
    second = cap.decision(handback(tmp_path), env(tmp_path))
    stopped = cap.decision(stop(tmp_path), env(tmp_path))

    assert kind(first) == "deny"
    assert first is not None
    output = first["hookSpecificOutput"]
    assert (output["hookEventName"], output["permissionDecision"]) == ("PreToolUse", "deny")
    assert f"- {path}: 9.0 KB, over its 8 KB cap" in output["permissionDecisionReason"]
    assert "call SubagentHandback again" in output["permissionDecisionReason"]
    assert (kind(second), kind(stopped)) == ("warn", "pass")


def test_a_handback_after_a_fix_passes_and_the_stop_stays_quiet(tmp_path, cap):
    path = grow(memory(tmp_path) / "commands.md", 1)
    begin(cap, tmp_path)
    grow(path, 9)
    transcript(tmp_path, task(appended()))
    assert kind(cap.decision(handback(tmp_path), env(tmp_path))) == "deny"
    grow(path, 7)

    assert kind(cap.decision(handback(tmp_path), env(tmp_path))) == "pass"
    assert kind(cap.decision(stop(tmp_path), env(tmp_path))) == "pass"


def test_a_denied_handback_counts_for_a_stop_without_another_handback(tmp_path, cap):
    path = grow(memory(tmp_path) / "commands.md", 1)
    begin(cap, tmp_path)
    grow(path, 9)
    transcript(tmp_path, task(appended()))
    cap.decision(handback(tmp_path), env(tmp_path))

    assert kind(cap.decision(stop(tmp_path), env(tmp_path))) == "warn"


def test_retro_runs_are_checked(tmp_path, cap):
    path = grow(memory(tmp_path) / "commands.md", 1)
    begin(cap, tmp_path)
    grow(path, 9)
    transcript(tmp_path, task(appended(), text="SHOP-412: Retro for SHOP-412."))

    assert kind(cap.decision(stop(tmp_path), env(tmp_path))) == "block"


@pytest.mark.parametrize(
    ("agent", "directory"),
    [
        (AGENT, "agent-harness-reviewer-qa"),
        (AGENT, "agent-harness-implementer-old"),
        ("Explore", "Explore"),
        (None, OWN),
    ],
)
def test_other_directories_and_agents_are_ignored(tmp_path, cap, agent, directory):
    grow(memory(tmp_path, directory) / "commands.md", 9)
    transcript(tmp_path, task(appended(directory=directory)))

    assert kind(cap.decision(stop(tmp_path, agent=agent), env(tmp_path))) == "pass"
    assert kind(cap.decision(handback(tmp_path, agent=agent), env(tmp_path))) == "pass"


def test_other_tools_are_not_checked(tmp_path, cap):
    grow(memory(tmp_path) / "commands.md", 9)
    transcript(tmp_path, task(appended()))

    assert kind(cap.decision(handback(tmp_path, tool="Bash"), env(tmp_path))) == "pass"


@pytest.mark.parametrize(
    ("settings", "kb", "blocked"),
    [
        ({"max_file_kb": 2}, 3, True),
        ({"max_file_kb": 16}, 9, False),
        ({"max_file_kb": "x"}, 9, True),
    ],
)
def test_caps_come_from_memory_json(tmp_path, cap, settings, kb, blocked):
    grow(memory(tmp_path) / "commands.md", kb)
    transcript(tmp_path, task(appended()))
    config = tmp_path / "harness" / "memory.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps(settings))

    assert (kind(cap.decision(stop(tmp_path), env(tmp_path))) == "block") is blocked


def test_an_unwritable_harness_home_lets_the_agent_through_and_says_so(tmp_path, cap):
    grow(memory(tmp_path) / "commands.md", 9)
    transcript(tmp_path, task(appended()))
    (tmp_path / "harness").write_text("a file, not a directory")

    begin(cap, tmp_path)
    for event in (stop(tmp_path), handback(tmp_path)):
        verdict = cap.decision(event, env(tmp_path))
        assert kind(verdict) == "warn"
        assert verdict is not None
        assert "could not run the memory cap check" in verdict["systemMessage"]
        assert "commands.md" in verdict["systemMessage"]


def run_hook(tmp_path: Path, payload: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        [sys.executable, str(HOOKS / "memory_cap.py")],
        input=payload,
        capture_output=True,
        text=True,
        env={**os.environ, "HOME": str(tmp_path / "home"), **env(tmp_path)},
        check=False,
    )


def test_the_script_records_the_start_and_blocks_the_stop(tmp_path):
    path = grow(memory(tmp_path) / "commands.md", 1)
    assert run_hook(tmp_path, json.dumps(started())).stdout == ""
    grow(path, 9)
    transcript(
        tmp_path, [prompt("SHOP-412: implement", 1, "p1"), *calls(appended(), minutes_ago=-1)]
    )

    result = run_hook(tmp_path, json.dumps(stop(tmp_path)))

    assert (result.returncode, result.stderr) == (0, "")
    assert json.loads(result.stdout)["decision"] == "block"
    assert "baseline" in json.loads(state(tmp_path).read_text())


@pytest.mark.parametrize("payload", ["not json", "[1, 2]"])
def test_an_internal_error_allows_the_stop(tmp_path, payload):
    result = run_hook(tmp_path, payload)

    assert (result.returncode, result.stdout) == (0, "")
    assert "memory_cap.py let the agent through after an internal error" in result.stderr
    assert "Traceback" not in result.stderr


def test_hooks_json_runs_the_check_at_the_start_the_stop_and_the_handback():
    config = json.loads((HOOKS / "hooks.json").read_text())

    def scripts(event: str, matcher: str | None) -> list[str]:
        return [
            handler["command"]
            for group in config["hooks"].get(event, [])
            if group.get("matcher") == matcher
            for handler in group["hooks"]
        ]

    for event, matcher in (
        ("SubagentStart", None),
        ("SubagentStop", None),
        ("PreToolUse", "SubagentHandback"),
        ("PostToolUse", "Edit|Write|MultiEdit"),
    ):
        assert any("hooks/memory_cap.py" in command for command in scripts(event, matcher))


GROWING_EDIT = {"old_string": "x", "new_string": "x\n- A lesson (source: SHOP-412, 2026-01-15)"}


def written(
    path: Path,
    tool: str = "Edit",
    agent: Any = AGENT,
    write_type: str = "update",
    cwd: str = "/work/shop",
    original: str | None = None,
    session: str = "/work/session.jsonl",
    **tool_input: Any,
) -> dict[str, Any]:
    response = {"filePath": str(path), "type": write_type}
    if original is not None:
        response["originalFile"] = original
    return {
        "hook_event_name": "PostToolUse",
        "tool_name": tool,
        "tool_input": {"file_path": str(path), **(tool_input or GROWING_EDIT)},
        "tool_response": response,
        "session_id": "s1",
        "agent_id": "a1",
        "agent_type": agent,
        "cwd": cwd,
        "transcript_path": session,
    }


LESSON = "- Run the specs with make spec (source: SHOP-412, 2026-01-15)\n"
LINK = "- [Commands](commands.md) — how to run the tests\n"


def noted(path: Path, kb: float, line: str = LESSON, head: str = "") -> Path:
    size = int(kb * 1024)
    body = head + line * ((size - len(head.encode())) // len(line.encode()))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body + "\n" * (size - len(body.encode())))
    return path


def context(verdict: dict[str, Any] | None) -> str:
    assert verdict is not None
    assert list(verdict) == ["hookSpecificOutput"]
    output = verdict["hookSpecificOutput"]
    assert list(output) == ["hookEventName", "additionalContext"]
    assert output["hookEventName"] == "PostToolUse"
    return str(output["additionalContext"])


@pytest.mark.parametrize(("name", "kb"), [("commands.md", 6), ("MEMORY.md", 3), ("t/a.md", 6.3)])
def test_a_write_under_the_warning_mark_adds_nothing(tmp_path, cap, name, kb):
    path = grow(memory(tmp_path) / name, kb)

    assert cap.decision(written(path), env(tmp_path)) is None
    assert not state(tmp_path).exists()


@pytest.mark.parametrize(
    ("name", "kb", "shown", "into"),
    [
        ("commands.md", 7, "is at 7.0 KB of its 8 KB cap (88 %)", "another topic file"),
        ("MEMORY.md", 3.5, "is at 3.5 KB of its 4 KB cap (88 %)", "a topic file"),
        ("topics/commands.md", 6.5, "is at 6.5 KB of its 8 KB cap (81 %)", "another topic file"),
    ],
)
def test_a_write_past_the_warning_mark_tells_the_agent_once(tmp_path, cap, name, kb, shown, into):
    path = noted(memory(tmp_path) / name, kb)

    text = context(cap.decision(written(path), env(tmp_path)))
    again = cap.decision(written(path), env(tmp_path))

    assert text == (
        f"agent-harness: {path} {shown}, past the 80 % warning mark. In this same run, move whole "
        f"lessons verbatim, with the same wording and the same `(source: …)`, into {into} linked "
        "from MEMORY.md, keeping one pointer line per topic in MEMORY.md, and list the moved "
        "lines in your report. A verbatim move is not a compaction and needs no approval. "
        "Rewording, merging or deleting a lesson always goes through the person."
    )
    assert again is None


@pytest.mark.parametrize(
    ("task_text", "retro"),
    [
        ("Retro for SHOP-412. Do not write to your memory yet.", True),
        ("SHOP-412: Retro for SHOP-412.", True),
        ("SHOP-412: implement it", False),
    ],
)
def test_a_retro_run_is_asked_to_propose_the_moves(tmp_path, cap, task_text, retro):
    path = noted(memory(tmp_path) / "MEMORY.md", 3.5)
    transcript(tmp_path, [prompt(task_text, 5, "p1")])

    event = written(path, session=str(tmp_path / "session.jsonl"))
    text = context(cap.decision(event, env(tmp_path)))

    proposal = (
        "This is a retro run: propose verbatim moves of whole lessons in your report, with the "
        "lines and the target topic file linked from MEMORY.md, for the person to approve; do "
        "not make a move yourself that the person has not approved. Rewording, merging or "
        "deleting a lesson always goes through the person."
    )
    assert (proposal in text) is retro
    assert ("In this same run, move whole lessons verbatim" in text) is not retro


@pytest.mark.parametrize(
    ("name", "kb", "content"),
    [
        ("MEMORY.md", 3.5, {"line": LINK}),
        ("MEMORY.md", 3.5, {"line": LINK, "head": LESSON}),
        ("commands.md", 7, {"line": "Prose that is not a lesson.\n"}),
    ],
)
@pytest.mark.parametrize("retro", [True, False])
def test_a_file_its_lessons_cannot_bring_under_the_mark_asks_for_fewer_topics(
    tmp_path, cap, name, kb, content, retro
):
    path = noted(memory(tmp_path) / name, kb, **content)
    task_text = "SHOP-412: implement it"
    if retro:
        task_text = "Retro for SHOP-412."
    transcript(tmp_path, [prompt(task_text, 5, "p1")])

    event = written(path, session=str(tmp_path / "session.jsonl"))
    text = context(cap.decision(event, env(tmp_path)))

    assert text.endswith(
        "past the 80 % warning mark. Moving its lessons out would not bring it under the mark: "
        "the rest is pointer lines and other text that is not a lesson. Propose in your report "
        "merging topics into fewer files or shortening the pointer hooks, for the person to "
        "approve; do not make those changes yourself. Rewording, merging or deleting a lesson "
        "always goes through the person."
    )
    assert "move whole lessons" not in text
    assert "verbatim moves" not in text


def test_lessons_that_bring_an_index_under_the_mark_are_moved(tmp_path, cap):
    path = noted(memory(tmp_path) / "MEMORY.md", 3.5, head=LINK * 20)

    text = context(cap.decision(written(path), env(tmp_path)))

    assert "In this same run, move whole lessons verbatim" in text


@pytest.mark.parametrize(("rest", "can_move"), [(4096, False), (4095, True)])
def test_the_lessons_must_bring_the_file_under_the_mark(tmp_path, cap, rest, can_move):
    path = memory(tmp_path) / "commands.md"
    path.write_text("#" * (rest - 1) + "\n" + LESSON * 10)
    config = tmp_path / "harness" / "memory.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"warn_ratio": 0.5}))

    text = context(cap.decision(written(path), env(tmp_path)))

    assert ("In this same run, move whole lessons verbatim" in text) is can_move
    assert ("Moving its lessons out would not bring it under the mark" in text) is not can_move


def test_another_file_gets_its_own_warning(tmp_path, cap):
    commands = grow(memory(tmp_path) / "commands.md", 7)
    index = grow(memory(tmp_path) / "MEMORY.md", 3.5)

    first = context(cap.decision(written(commands), env(tmp_path)))
    second = context(cap.decision(written(index), env(tmp_path)))

    assert str(commands) in first
    assert str(index) in second
    assert cap.decision(written(commands), env(tmp_path)) is None
    assert cap.decision(written(index), env(tmp_path)) is None


def test_a_new_run_is_warned_again_and_keeps_the_start_record(tmp_path, cap):
    path = grow(memory(tmp_path) / "commands.md", 7)
    begin(cap, tmp_path)
    context(cap.decision(written(path), env(tmp_path)))

    recorded = json.loads(state(tmp_path).read_text())
    begin(cap, tmp_path)

    assert recorded == {
        "baseline": {"commands.md": 7168},
        "started": cap.time.now,
        "warned": [str(path)],
    }
    context(cap.decision(written(path), env(tmp_path)))


SHRINKING_EDIT = {"old_string": "xx", "new_string": "x"}
CONTENT = {"content": "x"}


@pytest.mark.parametrize(
    ("tool", "start_kb", "write_type", "tool_input", "warned"),
    [
        ("Edit", 1, "update", GROWING_EDIT, True),
        ("Edit", 1, "update", SHRINKING_EDIT, False),
        ("Edit", 1, "update", {**SHRINKING_EDIT, "replace_all": True}, False),
        ("Edit", 1, "update", {"old_string": "x", "new_string": "y"}, False),
        ("MultiEdit", 1, "update", {"edits": [GROWING_EDIT, SHRINKING_EDIT]}, True),
        ("MultiEdit", 1, "update", {"edits": [SHRINKING_EDIT, SHRINKING_EDIT]}, False),
        ("MultiEdit", 1, "update", {"edits": "x"}, False),
        ("Edit", 1, "update", {"old_string": "abc", "new_string": "čř"}, True),
        ("Edit", 1, "update", {"old_string": "čř", "new_string": "abc"}, False),
        ("Write", 7.5, "update", CONTENT, False),
        ("Write", 7, "update", CONTENT, False),
        ("Write", 6.5, "update", CONTENT, True),
        ("Write", None, "update", CONTENT, True),
        ("Write", 7.5, "create", CONTENT, True),
    ],
)
def test_only_a_write_that_grew_the_file_is_warned_about(
    tmp_path, cap, tool, start_kb, write_type, tool_input, warned
):
    path = memory(tmp_path) / "commands.md"
    if start_kb is not None:
        grow(path, start_kb)
        begin(cap, tmp_path)
    grow(path, 7)

    verdict = cap.decision(written(path, tool, write_type=write_type, **tool_input), env(tmp_path))

    assert (verdict is not None) is warned
    if not warned:
        context(cap.decision(written(path), env(tmp_path)))


REMOVE_ALL = {"old_string": "x", "new_string": "", "replace_all": True}


@pytest.mark.parametrize(
    ("tool", "start_kb", "before_kb", "tool_input", "warned"),
    [
        ("Write", 7.5, 6, CONTENT, True),
        ("Write", 6.5, 7.8, CONTENT, False),
        ("MultiEdit", 1, 7.8, {"edits": [REMOVE_ALL, GROWING_EDIT]}, False),
        ("Edit", 1, 7, GROWING_EDIT, False),
        ("Edit", 1, 6.9, SHRINKING_EDIT, True),
    ],
)
def test_the_size_right_before_the_write_decides_when_the_tool_reports_it(
    tmp_path, cap, tool, start_kb, before_kb, tool_input, warned
):
    path = grow(memory(tmp_path) / "commands.md", start_kb)
    begin(cap, tmp_path)
    grow(path, 7)
    original = "x" * int(before_kb * 1024)

    verdict = cap.decision(written(path, tool, original=original, **tool_input), env(tmp_path))

    assert (verdict is not None) is warned


def test_the_original_file_counts_in_bytes(tmp_path, cap):
    path = grow(memory(tmp_path) / "commands.md", 7)

    verdict = cap.decision(written(path, original="č" * 3584 + "x"), env(tmp_path))

    assert verdict is None


@pytest.mark.parametrize(
    ("agent", "directory"),
    [
        (AGENT, "agent-harness-reviewer-qa"),
        (AGENT, "agent-harness-implementer-old"),
        ("Explore", "Explore"),
        ("general-purpose", OWN),
        (None, OWN),
    ],
)
def test_other_agents_and_directories_get_no_warning(tmp_path, cap, agent, directory):
    path = grow(memory(tmp_path, directory) / "commands.md", 7)

    assert cap.decision(written(path, agent=agent), env(tmp_path)) is None


def test_a_file_outside_the_memory_gets_no_warning(tmp_path, cap):
    path = grow(tmp_path / "work" / "shop" / "MEMORY.md", 7)

    assert cap.decision(written(path), env(tmp_path)) is None


@pytest.mark.parametrize("tool", ["Read", "NotebookEdit", "Bash"])
def test_other_tools_get_no_warning(tmp_path, cap, tool):
    path = grow(memory(tmp_path) / "commands.md", 7)

    assert cap.decision(written(path, tool), env(tmp_path)) is None


def test_a_relative_path_is_read_from_the_cwd(tmp_path, cap):
    path = grow(memory(tmp_path) / "commands.md", 7)

    verdict = cap.decision(written(Path("commands.md"), cwd=str(path.parent)), env(tmp_path))

    assert f"agent-harness: {path} is at 7.0 KB" in context(verdict)


@pytest.mark.parametrize(
    ("settings", "kb", "warned"),
    [
        ({"warn_ratio": 0.5}, 4.5, True),
        ({"warn_ratio": 0.5}, 4, True),
        ({"warn_ratio": 0.5}, 4095 / 1024, False),
        ({"warn_ratio": 0.5}, 3.5, False),
        ({"warn_ratio": 0.95}, 7, False),
        ({"warn_ratio": 0.95}, 7.8, True),
        ({"warn_ratio": 1}, 7.9, False),
        ({"max_file_kb": 16}, 7, False),
        ({"max_file_kb": 16, "warn_ratio": 0.4}, 7, True),
    ],
)
def test_the_warning_mark_comes_from_memory_json(tmp_path, cap, settings, kb, warned):
    path = grow(memory(tmp_path) / "commands.md", kb)
    config = tmp_path / "harness" / "memory.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps(settings))

    assert (cap.decision(written(path), env(tmp_path)) is not None) is warned


@pytest.mark.parametrize(
    "text",
    [
        '{"warn_ratio": 0}',
        '{"warn_ratio": -0.5}',
        '{"warn_ratio": 1.5}',
        '{"warn_ratio": NaN}',
        '{"warn_ratio": Infinity}',
        '{"warn_ratio": "0.5"}',
        '{"warn_ratio": true}',
        '{"warn_ratio": null}',
        "not json",
    ],
)
def test_an_unusable_warn_ratio_falls_back_to_the_default(tmp_path, cap, text):
    under = grow(memory(tmp_path) / "under.md", 6.3)
    over = grow(memory(tmp_path) / "over.md", 6.5)
    config = tmp_path / "harness" / "memory.json"
    config.parent.mkdir(parents=True)
    config.write_text(text)

    assert cap.decision(written(under), env(tmp_path)) is None
    assert "past the 80 % warning mark" in context(cap.decision(written(over), env(tmp_path)))


def test_an_unwritable_harness_home_warns_on_every_write(tmp_path, cap):
    path = grow(memory(tmp_path) / "commands.md", 7)
    (tmp_path / "harness").write_text("a file, not a directory")

    for _ in range(2):
        assert str(path) in context(cap.decision(written(path), env(tmp_path)))


def test_the_script_warns_once_after_a_write(tmp_path):
    path = grow(memory(tmp_path) / "commands.md", 7)

    first = run_hook(tmp_path, json.dumps(written(path)))
    second = run_hook(tmp_path, json.dumps(written(path)))

    assert (first.returncode, first.stderr) == (0, "")
    assert str(path) in context(json.loads(first.stdout))
    assert (second.returncode, second.stdout, second.stderr) == (0, "", "")
