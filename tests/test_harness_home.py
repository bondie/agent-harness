import importlib
from pathlib import Path

import pytest

from agent_harness import artifacts, memory

HOOKS = Path(__file__).resolve().parents[1] / "hooks"
HOOK_MODULES = (
    "save_artifacts",
    "quality_gate",
    "guard_shell",
    "guard_memory_writes",
    "guard_runaway",
)
DEFAULT = Path("/work/home/.claude/agent-harness")


@pytest.mark.parametrize(
    ("environ", "expected"),
    [
        ({}, DEFAULT),
        ({"HARNESS_HOME": ""}, DEFAULT),
        ({"HARNESS_HOME": "  "}, DEFAULT),
        ({"HARNESS_HOME": "/work/harness"}, Path("/work/harness")),
        ({"HARNESS_HOME": " /work/harness "}, Path("/work/harness")),
        ({"HARNESS_HOME": "~/harness"}, Path("/work/home/harness")),
    ],
)
def test_the_hooks_and_the_package_resolve_the_same_harness_home(environ, expected, monkeypatch):
    monkeypatch.syspath_prepend(str(HOOKS))
    monkeypatch.setenv("HOME", "/work/home")
    monkeypatch.delenv("HARNESS_HOME", raising=False)
    for name, setting in environ.items():
        monkeypatch.setenv(name, setting)
    hooks = [importlib.import_module(name) for name in HOOK_MODULES]

    assert artifacts.harness_home() == expected
    assert memory.harness_home() == expected
    assert [hook.harness_home(environ) for hook in hooks] == [expected] * len(hooks)
    assert importlib.import_module("log_agent_runs").log_path({}).parent == DEFAULT
