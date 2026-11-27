import importlib.util
import sys
from pathlib import Path

import pytest

from agent_harness import artifacts, memory

HOOKS = Path(__file__).resolve().parent.parent / "hooks"


def hook_module(name: str):
    spec = importlib.util.spec_from_file_location(f"hook_{name}", HOOKS / f"{name}.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    sys.path.insert(0, str(HOOKS))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(HOOKS))
    return module


def test_hooks_and_package_skip_the_same_standard_names() -> None:
    assert hook_module("save_artifacts").NOT_TICKET_PREFIXES == artifacts.NOT_TICKET_PREFIXES
    assert hook_module("session_state").NOT_TICKET_PREFIXES == artifacts.NOT_TICKET_PREFIXES


@pytest.mark.parametrize("agent_type", ["agent-harness:estimator", "estimator", "a b/c", ""])
def test_hooks_and_package_name_memory_directories_alike(agent_type, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    hook = hook_module("guard_memory_writes")
    assert memory.memory_dir_name(agent_type) == hook.memory_dir_name(agent_type)
    expected = memory.agent_memory_root() / memory.memory_dir_name(agent_type)
    assert hook_module("memory_cap").memory_dir(agent_type) == expected
