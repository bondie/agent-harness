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
    ):
        assert any("hooks/memory_cap.py" in command for command in scripts(event, matcher))
