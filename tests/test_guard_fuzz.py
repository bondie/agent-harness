"""The shell guard allows a call when it raises, so a crash on any input is a way past it."""

import os
from typing import Any

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tests.test_guard_shell import ENV, SUBAGENT, guard_shell

BRIDGE = "/work/no-harness-home/permissions/r1.decision.json"
TOKENS = [
    "echo",
    "cat",
    "rm",
    "-rf",
    "cp",
    "mv",
    "ln",
    "-s",
    "tee",
    "git",
    "push",
    "--force",
    "commit",
    "gh",
    "glab",
    "api",
    "-X",
    "POST",
    "find",
    "-delete",
    "-exec",
    "xargs",
    "sudo",
    "env",
    "nohup",
    "timeout",
    "5",
    "bash",
    "sh",
    "-c",
    "eval",
    "cd",
    "pushd",
    "popd",
    "dd",
    "of=x",
    "mysql",
    "-h",
    "--host=db",
    "security",
    "find-generic-password",
    "chmod",
    "+x",
    "install",
    "-t",
    "--target-directory=",
    "sed",
    "-i",
    "printenv",
    "A=1",
    "$HOME",
    "~",
    "..",
    "/",
    ".env",
    ".claude/settings.json",
    ".git/hooks/pre-commit",
    BRIDGE,
    "/work/no-harness-home/guard.json",
    ";",
    "&&",
    "||",
    "|",
    "&",
    ">",
    ">>",
    "<",
    "<>",
    "2>&1",
    "<<",
    "<<-",
    "EOF",
    "<<<",
    "(",
    ")",
    "{",
    "}",
    "$(",
    "`",
    "'",
    '"',
    "\\",
    "#",
    "\n",
    "*",
    "?",
    "[",
    "]",
    "!",
    "=",
    "--",
    "-",
]
SHELL_TEXT = st.one_of(
    st.text(max_size=200),
    st.lists(st.sampled_from(TOKENS), max_size=40).map(" ".join),
    st.lists(st.sampled_from(TOKENS), max_size=40).map("".join),
)
AGENTS = st.sampled_from([SUBAGENT, None])
settings.register_profile(
    "ci", max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow]
)
settings.register_profile(
    "deep", max_examples=20000, deadline=None, suppress_health_check=[HealthCheck.too_slow]
)
FUZZ = settings(settings.get_profile(os.environ.get("GUARD_FUZZ_PROFILE", "ci")))


def event(tool: str, key: str, value: str, agent: str | None, cwd: str) -> dict[str, Any]:
    data: dict[str, Any] = {"tool_name": tool, "tool_input": {key: value}, "cwd": cwd}
    if agent:
        data["agent_type"] = agent
    return data


@FUZZ
@given(command=SHELL_TEXT, agent=AGENTS, cwd=st.sampled_from(["/work/shop", "/", "relative"]))
def test_the_guard_never_raises_on_a_command(command, agent, cwd):
    guard_shell.check(event("Bash", "command", command, agent, cwd), ENV)


@FUZZ
@given(path=SHELL_TEXT, agent=AGENTS, tool=st.sampled_from(["Write", "Edit", "NotebookEdit"]))
def test_the_guard_never_raises_on_a_file_path(path, agent, tool):
    key = "notebook_path" if tool == "NotebookEdit" else "file_path"
    guard_shell.check(event(tool, key, path, agent, "/work/shop"), ENV)


def test_a_nul_character_is_denied_rather_than_crashing_the_guard():
    for agent in (SUBAGENT, None):
        denial, _ = guard_shell.check(event("Write", "file_path", "\0", agent, "/work/shop"), ENV)
        assert denial is not None
        assert denial.rule == "unparsable"
        denial, _ = guard_shell.check(
            event("Bash", "command", "cat a\0b", agent, "/work/shop"), ENV
        )
        assert denial is not None


SAFE_PREFIX = st.lists(
    st.sampled_from(["ls", "-la", "src", "echo", "ok", "cd", "/work/shop", ";", "&&", "||", "|"]),
    max_size=12,
).map(" ".join)


@FUZZ
@given(prefix=SAFE_PREFIX, agent=AGENTS)
def test_a_bridge_write_after_any_plain_chain_is_denied(prefix, agent):
    command = f"{prefix} ; echo x > {BRIDGE}"
    denial, _ = guard_shell.check(event("Bash", "command", command, agent, "/work/shop"), ENV)
    assert denial is not None
