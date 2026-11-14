"""Fuzz the shell guard: it must not raise, and the rules it promises must hold in any chain.

A crash denies a subagent's call (fail closed), so here it is a false denial, not a hole.
"""

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


PLAIN = st.lists(
    st.sampled_from(["ls -la src", "echo ok", "cd /work/shop", "true", "git status", "cat a.txt"]),
    max_size=4,
)
SEPARATORS = st.sampled_from([" ; ", " && ", " || ", " | ", "\n"])
PREFIXES = st.sampled_from(
    ["sudo {}", "env A=1 {}", "nohup {}", "timeout 5 {}", "command {}", "time {}"]
)
COMPOUND = st.sampled_from(["( {} )", "{{ {}; }}", "echo $({})"])
OUTER = st.sampled_from(
    [
        "{}",
        "! {}",
        "bash -c '{}'",
        'sh -c "{}"',
        "eval '{}'",
        "echo `{}`",
        "bash <<'EOF'\n{}\nEOF",
    ]
)
FORCE_PUSHES = [
    "git push --force",
    "git push -f",
    "git push origin +main",
    "git push --force origin main",
    "git push -fu origin topic",
    "git -C /work/shop push --force",
    "git push --force-with-lease",
    "git push --mirror",
    "git push origin :main",
    "git push --delete origin topic",
]
TAMPERING = [
    "echo x > .claude/settings.json",
    "echo x >> /work/shop/.claude/settings.json",
    "echo x > ~/.claude/settings.json",
    "echo x > ../shop/.claude/settings.local.json",
    "sed -i s/a/b/ ./.claude/settings.json",
    "dd of=.claude/settings.json",
    "echo x | tee .git/hooks/pre-commit",
    "cp a .git/hooks/pre-push",
    "mv a .git/hooks/post-checkout",
    "ln -s a .git/hooks/pre-commit",
    "install a .git/hooks/pre-commit",
    "git config core.hooksPath x",
]


@st.composite
def chained(draw: st.DrawFn, commands: list[str]) -> str:
    """One of `commands` in nested wrappers, after and before plain commands.

    A prefix such as `sudo` or `env` runs only a simple command, so it wraps before the rest.
    """
    command = draw(st.sampled_from(commands))
    for wrapper in draw(st.lists(PREFIXES, max_size=2)):
        command = wrapper.format(command)
    for wrapper in draw(st.lists(COMPOUND, max_size=2)):
        command = wrapper.format(command)
    command = draw(OUTER).format(command)
    before = draw(PLAIN)
    if before:
        command = draw(SEPARATORS).join(before) + draw(SEPARATORS) + command
    after = draw(PLAIN)
    if after:
        command += "\n" + " ; ".join(after)
    return command


@FUZZ
@given(command=chained(FORCE_PUSHES))
def test_a_force_push_anywhere_in_a_subagent_chain_is_denied_as_one(command):
    denial, _ = guard_shell.check(event("Bash", "command", command, SUBAGENT, "/work/shop"), ENV)
    assert denial is not None
    assert denial.rule in {"force-push", "force-with-lease", "push-delete"}


@FUZZ
@given(command=chained(TAMPERING))
def test_a_settings_or_git_hook_write_anywhere_in_a_subagent_chain_is_denied(command):
    denial, _ = guard_shell.check(event("Bash", "command", command, SUBAGENT, "/work/shop"), ENV)
    assert denial is not None
    assert denial.rule == "guard-tampering"


GUARDED_PATHS = st.builds(
    lambda base, slash, target: base + target.replace("/", slash, 1),
    st.sampled_from(["", "./", "/work/shop/", "sub/../", "../shop/", "/work/shop/sub/../"]),
    st.sampled_from(["/", "//", "/./"]),
    st.sampled_from(
        [
            ".claude/settings.json",
            ".claude/settings.local.json",
            ".git/hooks/pre-commit",
            ".git/hooks/post-checkout",
        ]
    ),
)


@FUZZ
@given(path=GUARDED_PATHS, tool=st.sampled_from(["Write", "Edit", "NotebookEdit"]))
def test_a_file_tool_write_to_settings_or_git_hooks_is_denied(path, tool):
    key = "notebook_path" if tool == "NotebookEdit" else "file_path"
    denial, _ = guard_shell.check(event(tool, key, path, SUBAGENT, "/work/shop"), ENV)
    assert denial is not None
    assert denial.rule == "guard-tampering"
