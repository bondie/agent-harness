import importlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import numpy as np
import pytest

HOOKS = Path(__file__).resolve().parents[1] / "hooks"
AGENT = "agent-harness:implementer"
OWN = "/work/home/.claude/agent-memory/agent-harness-implementer"
LOCAL = "/work/shop/.claude/agent-memory-local/agent-harness-implementer"
OTHER = "/work/home/.claude/agent-memory/agent-harness-reviewer-qa"
FILES = (
    f"{OWN}/MEMORY.md",
    f"{OWN}/commands.md",
    f"{LOCAL}/traps.md",
    f"{OTHER}/commands.md",
    f"{OWN}-old/commands.md",
    "/work/shop/src/app.py",
)
INDEX = "# Implementer memory\n\n- [Commands](commands.md) — how to run the tests"
MEMORY_PROMPT = f"# Persistent Agent Memory\n\nYou have a memory system at `{OWN}/`.\n"
WINDOW = 3


@pytest.fixture
def log_runs(monkeypatch, tmp_path) -> ModuleType:
    monkeypatch.syspath_prepend(str(HOOKS))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home" / ".claude" / "agent-memory" / "agent-harness-implementer").mkdir(
        parents=True
    )
    return importlib.import_module("log_agent_runs")


def entry(kind: str, **fields: Any) -> dict[str, Any]:
    return {"type": kind, "timestamp": "2026-01-01T10:00:00Z", **fields}


def snapshot(*parts: Any) -> dict[str, Any]:
    attachment = {"type": "prompt_snapshot", "systemPrompt": list(parts)}
    return entry("attachment", attachment=attachment)


def use(call: str, tool: str, path: str) -> dict[str, Any]:
    tool_input = {"command": f"sed -n 1,40p {path}"} if tool == "Bash" else {"file_path": path}
    return {"type": "tool_use", "id": call, "name": tool, "input": tool_input}


def result(call: str, size: int, blocks: bool) -> dict[str, Any]:
    content: Any = "x" * size
    if blocks:
        content = [
            {"type": "text", "text": "x" * (size // 2)},
            {"type": "text", "text": "x" * (size - size // 2)},
        ]
    return {"type": "tool_result", "tool_use_id": call, "content": content}


def write_transcript(path: Path, entries: list[dict[str, Any]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(item) + "\n" for item in entries))
    return path


def is_own(path: str) -> bool:
    return path.startswith((f"{OWN}/", f"{LOCAL}/"))


@pytest.mark.parametrize("seed", range(6))
def test_memory_reads_and_bash_match_a_brute_force_count(tmp_path, log_runs, seed):
    rng = np.random.default_rng(seed)
    calls = []
    for number in range(60):
        tool = ("Read", "Read", "Edit", "Write", "MultiEdit", "Bash")[int(rng.integers(0, 6))]
        path = FILES[int(rng.integers(0, len(FILES)))]
        size = int(rng.integers(0, 400)) if rng.random() > 0.1 else None
        calls.append((f"t{number}", tool, path, size))
    entries = [entry("user", message={"content": "SHOP-412: implement"})]
    position = 0
    sent: list[dict[str, Any]] = []
    while position < len(calls):
        batch = calls[position : position + int(rng.integers(1, 4))]
        position += len(batch)
        uses = [use(call, tool, path) for call, tool, path, _ in batch]
        if rng.random() < 0.3:
            ahead = [uses[int(i)] for i in rng.permutation(len(uses))]
            early = {"type": "api-request-blob", "message": {"role": "assistant", "content": ahead}}
            entries.append(early)
        entries.append(entry("assistant", message={"content": uses}))
        answered = [
            result(call, size, rng.random() < 0.3) for call, _, _, size in batch if size is not None
        ]
        rng.shuffle(answered)
        entries.append(entry("user", message={"content": answered}))
        sent += [*uses, *answered]
        if rng.random() < 0.4:
            entries.append(entry("assistant", message={"content": uses}))
        if rng.random() < 0.5:
            replayed = [sent[int(i)] for i in rng.integers(0, len(sent), int(rng.integers(1, 8)))]
            role = ("assistant", "user")[int(rng.integers(0, 2))]
            blob = {"type": "api-request-blob", "message": {"role": role, "content": replayed}}
            entries.append(blob)
    reads = 0
    for index, (_, tool, path, size) in enumerate(calls):
        for_edit = any(
            later_tool in ("Edit", "Write", "MultiEdit") and later_path == path
            for _, later_tool, later_path, _ in calls[index + 1 : index + 1 + WINDOW]
        )
        if tool == "Read" and is_own(path) and not for_edit:
            reads += size or 0
    bash = sum(size or 0 for _, tool, path, size in calls if tool == "Bash" and is_own(path))

    summary = log_runs.summarise(write_transcript(tmp_path / "agent.jsonl", entries), AGENT)

    assert (summary["memory_read_chars"], summary["memory_bash_chars"]) == (reads, bash)
    in_assistant = [i for e in entries if e["type"] == "assistant" for i in e["message"]["content"]]
    assert summary["tool_uses"] == len(in_assistant)
    assert log_runs.READ_FOR_EDIT_CALLS == WINDOW


def test_only_a_read_shortly_before_an_edit_of_the_file_is_not_counted(tmp_path, log_runs):
    topic, other = f"{OWN}/commands.md", "/work/shop/src/app.py"
    made = [
        ("r1", "Read", topic, 100),
        ("e1", "Edit", topic, 10),
        ("r2", "Read", topic, 40),
        ("x1", "Read", other, 1),
        ("x2", "Read", other, 1),
        ("e2", "MultiEdit", topic, 10),
        ("r3", "Read", topic, 7),
        ("x3", "Read", other, 1),
        ("x4", "Read", other, 1),
        ("x5", "Read", other, 1),
        ("e3", "Write", topic, 10),
        ("r4", "Read", f"{OTHER}/commands.md", 70),
        ("b1", "Bash", topic, 30),
    ]
    entries = []
    for call, tool, path, size in made:
        entries.append(entry("assistant", message={"content": [use(call, tool, path)]}))
        entries.append(entry("user", message={"content": [result(call, size, call == "r3")]}))
        if call == "r3":
            entries.append(entry("assistant", message={"content": [use("e2", "MultiEdit", topic)]}))
            replay = [use("e1", "Edit", topic), result("r1", 100, False)]
            entries.append({"type": "api-request-blob", "message": {"content": replay}})

    summary = log_runs.summarise(write_transcript(tmp_path / "agent.jsonl", entries), AGENT)

    assert summary["memory_read_chars"] == 7
    assert summary["memory_bash_chars"] == 30


@pytest.mark.parametrize(
    ("command", "counted"),
    [
        (f"cd {OWN} && cat general.md", True),
        ("cat ~/.claude/agent-memory/agent-harness-implementer/general.md", True),
        (f"python3 - <<'EOF'\nopen('{LOCAL}/traps.md').read()\nEOF", True),
        (f"cat {OTHER}/general.md", False),
        (f"cat {OWN}-old/general.md", False),
        ("cat agent-harness-implementer/general.md", False),
        ("uv run pytest -q", False),
    ],
)
def test_bash_counts_commands_that_name_the_own_memory_directory(
    tmp_path, log_runs, command, counted
):
    call = {"type": "tool_use", "id": "b1", "name": "Bash", "input": {"command": command}}
    entries = [
        entry("assistant", message={"content": [call]}),
        entry("user", message={"content": [result("b1", 25, False)]}),
    ]

    summary = log_runs.summarise(write_transcript(tmp_path / "agent.jsonl", entries), AGENT)

    assert summary["memory_bash_chars"] == (25 if counted else 0)


@pytest.mark.parametrize(
    "parts",
    [
        ("You are the implementer.", f"{MEMORY_PROMPT}\n## MEMORY.md\n\n{INDEX}\n\n"),
        (f"{MEMORY_PROMPT}## MEMORY.md\n{INDEX}", "Notes", "Environment"),
    ],
)
def test_the_injected_index_is_read_from_the_prompt_snapshot(tmp_path, log_runs, parts):
    later = snapshot(f"{MEMORY_PROMPT}## MEMORY.md\nlater")
    entries = [snapshot(*parts), later, entry("user", message={})]

    summary = log_runs.summarise(write_transcript(tmp_path / "agent.jsonl", entries), AGENT)

    assert summary["memory_injected_chars"] == len(INDEX)


@pytest.mark.parametrize(
    "entries",
    [
        [],
        [snapshot("A prompt without a memory section")],
        [snapshot(f"{MEMORY_PROMPT.replace('implementer', 'reviewer-qa')}## MEMORY.md\n{INDEX}")],
        [entry("attachment", attachment={"type": "prompt_snapshot", "systemPrompt": "text"})],
        [entry("attachment", attachment={"type": "prompt_snapshot", "systemPrompt": [3, None]})],
        [entry("attachment", attachment="prompt_snapshot")],
    ],
)
def test_without_a_usable_snapshot_the_index_on_disk_is_used(
    tmp_path, log_runs, monkeypatch, entries
):
    home = tmp_path / "home"
    index = home / ".claude" / "agent-memory" / "agent-harness-implementer" / "MEMORY.md"
    index.parent.mkdir(parents=True, exist_ok=True)
    index.write_text(INDEX + "\n")
    monkeypatch.setenv("HOME", str(home))
    path = write_transcript(tmp_path / "agent.jsonl", [entry("user", message={}), *entries])

    assert log_runs.summarise(path, AGENT)["memory_injected_chars"] == len(INDEX) + 1
    index.unlink()
    summary = log_runs.summarise(path, AGENT)
    assert "memory_injected_chars" not in summary
    assert (summary["memory_read_chars"], summary["memory_bash_chars"]) == (0, 0)


def memory_fields(summary: dict[str, Any]) -> list[str]:
    return [field for field in summary if field.startswith("memory_")]


def test_an_unknown_agent_logs_no_memory_fields(tmp_path, log_runs):
    path = write_transcript(tmp_path / "agent.jsonl", [snapshot(f"## MEMORY.md\n{INDEX}")])

    assert memory_fields(log_runs.summarise(path)) == []


@pytest.mark.parametrize(
    ("agent", "entries"),
    [
        ("Explore", [entry("user", message={"content": "Find the export code"})]),
        ("general-purpose", [snapshot("You are an agent.", "Environment")]),
        (
            "fork",
            [
                snapshot(
                    f"{MEMORY_PROMPT.replace('implementer', 'jira-analyst')}## MEMORY.md\n{INDEX}"
                )
            ],
        ),
    ],
)
def test_an_agent_without_its_own_memory_logs_no_memory_fields(tmp_path, log_runs, agent, entries):
    read = entry("assistant", message={"content": [use("r1", "Read", f"{OWN}/commands.md")]})
    path = write_transcript(tmp_path / "agent.jsonl", [*entries, read])

    assert memory_fields(log_runs.summarise(path, agent)) == []


def test_a_prompt_that_names_the_memory_is_enough_without_the_directory(tmp_path, log_runs):
    shutil.rmtree(tmp_path / "home")
    path = write_transcript(
        tmp_path / "agent.jsonl", [snapshot(f"{MEMORY_PROMPT}## MEMORY.md\n{INDEX}")]
    )

    summary = log_runs.summarise(path, AGENT)

    assert summary["memory_injected_chars"] == len(INDEX)


def test_a_logged_run_carries_the_memory_fields(tmp_path):
    home = tmp_path / "home"
    own = home / ".claude" / "agent-memory" / "agent-harness-implementer"
    own.mkdir(parents=True)
    entries = [
        snapshot(f"memory at `{own}/`\n## MEMORY.md\n{INDEX}"),
        entry("assistant", message={"content": [use("r1", "Read", f"{own}/commands.md")]}),
        entry("user", message={"content": [result("r1", 120, False)]}),
    ]
    transcript = write_transcript(tmp_path / "subagents" / "agent-a1.jsonl", entries)
    log = tmp_path / "runs.jsonl"
    event = {"agent_id": "a1", "agent_type": AGENT, "agent_transcript_path": str(transcript)}

    subprocess.run(  # noqa: S603
        [sys.executable, str(HOOKS / "log_agent_runs.py")],
        input=json.dumps(event),
        text=True,
        env={**os.environ, "HOME": str(home), "HARNESS_RUNS_LOG": str(log)},
        check=True,
    )

    [run] = [json.loads(line) for line in log.read_text().splitlines()]
    assert run["memory_injected_chars"] == len(INDEX)
    assert (run["memory_read_chars"], run["memory_bash_chars"]) == (120, 0)


def test_without_the_guard_hook_the_run_is_logged_without_memory_fields(tmp_path):
    plugin = tmp_path / "plugin" / "hooks"
    plugin.mkdir(parents=True)
    shutil.copy(HOOKS / "log_agent_runs.py", plugin)
    home = tmp_path / "home"
    own = home / ".claude" / "agent-memory" / "agent-harness-implementer"
    own.mkdir(parents=True)
    entries = [
        snapshot(f"memory at `{own}/`\n## MEMORY.md\n{INDEX}"),
        entry("assistant", message={"content": [use("r1", "Read", f"{own}/commands.md")]}),
        entry("user", message={"content": [result("r1", 120, False)]}),
    ]
    transcript = write_transcript(tmp_path / "subagents" / "agent-a1.jsonl", entries)
    log = tmp_path / "runs.jsonl"
    event = {"agent_id": "a1", "agent_type": AGENT, "agent_transcript_path": str(transcript)}

    result_ = subprocess.run(  # noqa: S603
        [sys.executable, str(plugin / "log_agent_runs.py")],
        input=json.dumps(event),
        capture_output=True,
        text=True,
        env={**os.environ, "HOME": str(home), "HARNESS_RUNS_LOG": str(log)},
        check=False,
    )

    assert (result_.returncode, result_.stderr) == (0, "")
    [run] = [json.loads(line) for line in log.read_text().splitlines()]
    assert run["tool_uses"] == 1
    assert memory_fields(run) == []
