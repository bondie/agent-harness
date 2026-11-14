#!/usr/bin/env python3
"""PreToolUse hook: deny commands that destroy work, leak secrets, touch production or the guard.

It parses the Bash command, including chains, pipes, subshells, `bash -c`, `eval`, `xargs`,
`sudo`/`env` prefixes and heredocs fed to a shell, and denies with the rule and a safe
alternative: force-pushes and pushes that delete remote refs, `git reset --hard`/`clean -f`/
`checkout .`/`checkout -f`/`restore .` and `git branch -D` outside the allowed temp dirs,
writes to the history, the remote or the forge from a subagent (`git commit`, `push`, `merge`,
`tag`, `rebase`, `cherry-pick`, `revert`, `am`; `gh`/`glab` merge requests, releases and API
writes) unless its type is in `git_write_allow_agents` or the repository is inside the temp
dirs, `rm -r` and `find -delete`/`-exec rm` outside the allowed dirs (temp dirs, build and cache
dirs in the project, `allowed_rm_dirs`), raw reads of secret files (also through `<`), copies
of them, environment dumps, macOS keychain reads, connections to production database hosts,
DROP/TRUNCATE in or piped into a database client command, and a `$(` never closed. Shell
writes and Write/Edit/MultiEdit/NotebookEdit calls that change Claude Code settings, the
guard's own settings, git hooks or installed plugins are denied too, as is `core.hooksPath`;
the agent types in `tampering_allow_agents` may change the user's Claude Code settings.json.
Writes to the permission bridge's folder are denied in every session, the main one included.
Each denial appends a line without the command to `$HARNESS_HOME/guard-events.jsonl`.
Settings: built-in defaults, then `$HARNESS_HOME/guard.json` (default
`~/.claude/agent-harness/guard.json`), then the `guard` key of the project's
`.claude/agent-harness.json`. It catches mistakes; it is not a sandbox against deliberate
evasion. Standard library only; on an internal error it denies a subagent's call and allows
the main session's, logging to stderr.
"""

from __future__ import annotations

import glob
import json
import os
import re
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from fnmatch import fnmatchcase
from pathlib import Path, PurePath
from typing import Any, TextIO
from urllib.parse import urlsplit

DEFAULTS: dict[str, Any] = {
    "scope": "subagents",
    "allow_force_with_lease": "main",
    "secret_globs": [
        ".env",
        ".env.*",
        "*.pem",
        "*.key",
        "id_rsa*",
        "id_ed25519*",
        ".npmrc",
        ".pypirc",
        ".netrc",
        "credentials",
        "credentials.json",
        "*credentials.y*ml",
        ".aws/credentials",
        "*.credentials",
        "auth.json",
    ],
    "secret_allow_globs": [".env.example", ".env.dist", "*.pub"],
    "allowed_rm_dirs": [],
    "prod_db_hosts": [],
    "safe_filters": [],
    "extra_deny": [],
    "tampering_allow_agents": ["statusline-setup"],
    "git_write_allow_agents": [],
}
LIST_KEYS = {
    "secret_globs",
    "secret_allow_globs",
    "allowed_rm_dirs",
    "prod_db_hosts",
    "safe_filters",
    "extra_deny",
    "tampering_allow_agents",
    "git_write_allow_agents",
}
REGEX_KEYS = {"prod_db_hosts", "safe_filters"}
# Settings of other guard hooks that share this config; they validate their own.
OTHER_HOOK_KEYS = {"runaway", "permission_bridge"}
DEFAULT_RM_DIRS = ["node_modules", "var/cache", "dist", "build", "coverage", ".pytest_cache"]
DEFAULT_RM_DIRS += ["__pycache__", ".mypy_cache", ".ruff_cache"]
SCRATCHPADS = ["/tmp/claude-*", "/private/tmp/claude-*"]  # noqa: S108
MAX_DEPTH = 8
# Each `cd` after `;` can double the possible directories; beyond this they count as unknown.
MAX_DIRS = 16

REDIRECTS = {"<<<", "<<-", "<<", "<>", "<&", ">>", ">&", ">|", "&>>", "&>", "<", ">"}
OPERATORS = sorted(
    [*REDIRECTS, "&&", "||", ";;", "|&", ";", "&", "|", "(", ")", "\n"], key=len, reverse=True
)
PIPES = {"|", "|&"}
ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
VARIABLE = re.compile(r"\$\{(\w+)\}|\$(\w+)")
MAGIC = re.compile(r"[*?[]")
RESERVED = {"{", "}", "!", "if", "then", "else", "elif", "fi", "do", "done", "while", "until"}
RESERVED |= {"case", "esac", "for", "select", "function", "in"}
WRAPPERS: dict[str, set[str]] = {
    "sudo": {"-u", "-g", "-h", "-p", "-C", "-D", "-r", "-t", "-U", "-T"},
    "doas": {"-u", "-C"},
    "env": {"-u", "-C", "-S", "--unset", "--chdir", "--split-string"},
    "command": set(),
    "builtin": set(),
    "exec": {"-a"},
    "nohup": set(),
    "time": set(),
    "nice": {"-n"},
    "timeout": {"-s", "-k", "--signal", "--kill-after"},
    "stdbuf": {"-i", "-o", "-e"},
    "ionice": {"-c", "-n", "-p"},
    "xargs": {"-I", "-L", "-n", "-P", "-s", "-d", "-E", "-a", "--max-args", "--delimiter"},
}
SHELLS = {"bash", "sh", "zsh", "dash", "ksh"}
GREPS = {"grep", "egrep", "fgrep"}
READERS = GREPS | {"cat", "head", "tail", "less", "more", "sed", "awk", "gawk", "strings"}
READERS |= {"xxd", "base64", "rg", "bat", "nl", "tac", "jq", "od", "hexdump", "uuencode"}
READERS |= {"cut", "sort", "uniq", "paste", "rev", "fold", "fmt", "pr", "comm", "join", "column"}
READERS |= {"expand", "unexpand", "wc"}
GREP_QUIET = {"-c", "-l", "-L", "-q", "--count", "--files-with-matches"}
GREP_QUIET |= {"--files-without-match", "--quiet", "--silent"}
GREP_LONG_VALUES = {"--regexp", "--file", "--max-count", "--after-context", "--before-context"}
GREP_LONG_VALUES |= {"--context", "--include", "--exclude", "--exclude-dir", "--label"}
RG_QUIET = GREP_QUIET | {"--files", "--count-matches"}
RG_LONG_VALUES = GREP_LONG_VALUES | {"--glob", "--iglob", "--type", "--type-not", "--replace"}
RG_LONG_VALUES |= {"--max-depth", "--encoding", "--threads", "--max-columns", "--pre"}
JQ_PAIRS = {"--arg", "--argjson", "--slurpfile", "--rawfile"}
DB_CLIENTS = {"mysql", "mariadb", "psql", "mongosh", "mongo", "redis-cli"}
DB_HOST_ENV = {"PGHOST", "MYSQL_HOST"}
DESTRUCTIVE_SQL = re.compile(r"\b(?:DROP\s+(?:DATABASE|TABLE|SCHEMA)\b|TRUNCATE\b(?!\s*\())", re.I)
GIT_VALUE_OPTIONS = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--config-env"}
GIT_WRITES = {"commit", "push", "merge", "rebase", "cherry-pick", "revert", "am"}
GIT_WRITES |= {"commit-tree", "update-ref", "filter-branch", "filter-repo", "fast-import"}
FETCH_VALUES = {"--depth", "--deepen", "--shallow-since", "--shallow-exclude", "--upload-pack"}
FETCH_VALUES |= {"--server-option", "--refmap", "--negotiation-tip", "--jobs"}
SUBTREE_WRITES = {"push", "merge", "pull", "add"}
SUBTREE_VALUES = {"--prefix", "--message", "--onto", "--branch"}
TAG_LISTS = {"-l", "--list", "--contains", "--no-contains", "--merged", "--no-merged"}
TAG_LISTS |= {"--points-at", "-v", "--verify", "-n", "--column"}
ABORTABLE = {"merge", "rebase", "cherry-pick", "am", "revert"}
RESET_VALUES = {"--pathspec-from-file"}
TAG_VALUES = {"--message", "--file", "--local-user", "--sort", "--format", "--cleanup"}
PUSH_VALUES = {"--repo", "--push-option", "--receive-pack", "--exec"}
FORGE_WRITES: dict[str, dict[str, set[str]]] = {
    "gh": {
        "pr": {"create", "new", "merge", "close", "reopen", "edit", "ready"},
        "release": {"create", "delete", "edit", "upload"},
        "repo": {"delete", "create", "edit", "rename", "archive", "sync"},
        "workflow": {"run"},
        "secret": {"set", "delete"},
        "variable": {"set", "delete"},
        "gist": {"create", "new", "edit", "delete"},
    },
    "glab": {
        "mr": {"create", "new", "merge", "accept", "close", "reopen", "update", "approve"}
        | {"unapprove", "rebase"},
        "release": {"create", "delete", "upload"},
        "repo": {"delete"},
        "ci": {"run", "retry", "cancel"},
        "variable": {"set", "update", "delete"},
    },
}
FORGE_VALUES = {"-R", "--repo"}
API_VALUES = {"--method", "--header", "--raw-field", "--field", "--jq", "--template"}
API_VALUES |= {"--preview", "--input", "--cache", "--hostname"}
API_FIELDS = {"-f", "-F", "--field", "--raw-field", "--input"}
WHOLE_TREE = {".", "./", "*", ":/", ":/.", ":/*", ":(top)", ":(top)."}
CONFIG_READS = {"--get", "--get-all", "--get-regexp", "--list", "-l", "get", "list"}
CONFIG_CHANGES = {"--unset", "--unset-all", "--add", "--replace-all", "unset"}
TARGET_VALUES = {"--target-directory", "--suffix"}
RSYNC_VALUES = {"--rsh", "--filter", "--exclude", "--include", "--exclude-from", "--include-from"}
RSYNC_VALUES |= {"--files-from", "--temp-dir", "--partial-dir", "--compare-dest", "--copy-dest"}
RSYNC_VALUES |= {"--link-dest", "--backup-dir", "--suffix", "--chmod", "--chown", "--log-file"}
RSYNC_VALUES |= {"--rsync-path", "--password-file", "--timeout", "--port", "--bwlimit"}
# Per program: short options and long options that take a value.
COPIERS: dict[str, tuple[str, set[str]]] = {
    "cp": ("tS", TARGET_VALUES),
    "mv": ("tS", TARGET_VALUES),
    "ln": ("tS", TARGET_VALUES),
    "install": ("tSmogB", TARGET_VALUES | {"--mode", "--owner", "--group"}),
    "ditto": ("", {"--arch", "--bom"}),
    "scp": ("cDFiJloPSX", set()),
    "rsync": ("eBfTM", RSYNC_VALUES),
}
SECRET_COPIERS = {"cp", "mv", "install", "ditto", "scp", "rsync", "dd"}
CHANGERS = {"rm", "unlink", "shred", "truncate", "chmod", "tee", "sponge"}
WRITES = {">", ">>", ">|", "&>", "&>>", ">&", "<>"}
SECRET_NAME = re.compile(
    r"(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|PASSPHRASE|PASS|AUTH|CREDENTIAL)S?(?:_|$)", re.I
)
KEYCHAIN_READS = {"find-generic-password", "find-internet-password", "dump-keychain"}
FILE_TOOLS = {"Write": "file_path", "Edit": "file_path", "MultiEdit": "file_path"}
FILE_TOOLS["NotebookEdit"] = "notebook_path"
CUT_VALUES = {"--bytes", "--characters", "--delimiter", "--fields", "--output-delimiter"}
CUT_NAMES_ONLY = {"-s", "--only-delimited", "-d", "--delimiter", "-f", "--fields"}
REPO_GUARDS = ["*/.claude/settings.json", "*/.claude/settings.local.json"]
REPO_GUARDS += ["*/.claude/agent-harness.json", "*/.git/hooks", "*/.git/hooks/*"]
PRINT_PROCEED = (
    "show key names or masked values instead (a pipe into `cut -s -d= -f1`, or a script that "
    "parses the file and prints its keys), count or list matches with `grep -c`/`-l`/`-q`, "
    "or pipe the output through a filter listed in safe_filters."
)
TAMPER_PROCEED = (
    "leave it to the person and put the change you need in your report; a copy inside the "
    "temp dir or scratchpad may be changed."
)
GIT_WRITE_PROCEED = (
    "leave the commit, push or MR to the team lead and put it in your report. A repository "
    "inside the temp dir or scratchpad may be committed to, and pushed to a path there."
)


@dataclass
class Config:
    scope: str
    allow_force_with_lease: bool | str
    secret_globs: list[str]
    secret_allow_globs: list[str]
    rm_dirs: list[str]
    rm_trees: list[str]
    prod_db_hosts: list[re.Pattern[str]]
    safe_filters: list[re.Pattern[str]]
    extra_deny: list[tuple[re.Pattern[str], str]]
    tampering_allow_agents: list[str]
    git_write_allow_agents: list[str]
    warnings: list[str]


def expand(text: str, environ: Mapping[str, str]) -> str:
    text = VARIABLE.sub(lambda m: environ.get(m.group(1) or m.group(2), m.group(0)), text)
    home = environ.get("HOME")
    if home and (text == "~" or text.startswith("~/")):
        return home + text[1:]
    return text


def read_json(path: Path, warnings: list[str]) -> Any:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as error:
        warnings.append(f"{path} is unreadable ({error.strerror})")
        return None
    try:
        return json.loads(text)
    except ValueError:
        warnings.append(f"{path} is not valid JSON")
        return None


def harness_home(environ: Mapping[str, str]) -> Path:
    custom = environ.get("HARNESS_HOME", "").strip()
    if custom:
        return Path(custom).expanduser()
    return Path.home() / ".claude" / "agent-harness"


def config_sources(
    environ: Mapping[str, str], project: Path | None, warnings: list[str]
) -> list[tuple[str, Any]]:
    user_path = harness_home(environ) / "guard.json"
    sources: list[tuple[str, Any]] = []
    user = read_json(user_path, warnings)
    if user is not None:
        sources.append((str(user_path), user))
    if project is None:
        return sources
    path = project / ".claude" / "agent-harness.json"
    data = read_json(path, warnings)
    if data is None:
        return sources
    if not isinstance(data, dict):
        warnings.append(f"{path} is not a JSON object")
        return sources
    if "guard" in data:
        sources.append((f"{path} (guard)", data["guard"]))
    return sources


def compiles(pattern: Any) -> bool:
    if not isinstance(pattern, str):
        return False
    try:
        re.compile(pattern)
    except re.error:
        return False
    return True


def problem(key: str, value: Any) -> str | None:
    if key == "scope":
        return None if value in ("subagents", "all") else 'must be "subagents" or "all"'
    if key == "allow_force_with_lease":
        return (
            None if isinstance(value, bool) or value == "main" else 'must be true, false or "main"'
        )
    if not isinstance(value, list):
        return "must be a list"
    if key == "extra_deny":
        valid = all(
            isinstance(item, dict)
            and compiles(item.get("pattern"))
            and isinstance(item.get("reason"), str)
            for item in value
        )
        return None if valid else 'needs {"pattern": <regex>, "reason": <text>} items'
    if key in REGEX_KEYS:
        return None if all(compiles(item) for item in value) else "needs valid regular expressions"
    return None if all(isinstance(item, str) for item in value) else "needs strings"


def dir_pattern(entry: str, environ: Mapping[str, str], project: Path | None) -> str | None:
    text = expand(entry, environ)
    if text.startswith("~"):
        return None
    path = PurePath(text)
    if not path.is_absolute():
        if project is None:
            return None
        path = project / path
    parts = path.parts
    fixed = next((i for i, part in enumerate(parts) if MAGIC.search(part)), len(parts))
    head = os.path.realpath(PurePath(*parts[:fixed]))
    return str(PurePath(glob.escape(head), *parts[fixed:]))


def temp_dirs(environ: Mapping[str, str]) -> list[str]:
    dirs = [environ.get(name, "") for name in ("TMPDIR", "TEMP", "TMP")]
    return [d for d in dirs if d] or ["/tmp"]  # noqa: S108


def load_config(environ: Mapping[str, str], project: Path | None) -> Config:
    warnings: list[str] = []
    settings: dict[str, Any] = {
        key: list(value) if isinstance(value, list) else value for key, value in DEFAULTS.items()
    }
    for name, data in config_sources(environ, project, warnings):
        if not isinstance(data, dict):
            warnings.append(f"{name} is not a JSON object")
            continue
        for key, value in data.items():
            if key in OTHER_HOOK_KEYS:
                continue
            if key not in DEFAULTS:
                warnings.append(f"{name}: unknown key {key}")
                continue
            issue = problem(key, value)
            if issue:
                warnings.append(f"{name}: {key} {issue}, so it is ignored")
                continue
            settings[key] = settings[key] + value if key in LIST_KEYS else value
    rm_dirs = [dir_pattern(entry, environ, None) for entry in SCRATCHPADS + temp_dirs(environ)]
    rm_trees = [dir_pattern(entry, environ, project) for entry in settings["allowed_rm_dirs"]]
    if project is not None:
        root = glob.escape(os.path.realpath(project))
        rm_trees += [str(PurePath(root, name)) for name in DEFAULT_RM_DIRS]
        rm_trees += [str(PurePath(root, "*", name)) for name in DEFAULT_RM_DIRS]
    return Config(
        scope=settings["scope"],
        allow_force_with_lease=settings["allow_force_with_lease"],
        secret_globs=settings["secret_globs"],
        secret_allow_globs=settings["secret_allow_globs"],
        rm_dirs=[pattern for pattern in rm_dirs if pattern],
        rm_trees=[pattern for pattern in rm_trees if pattern],
        prod_db_hosts=[re.compile(p, re.I) for p in settings["prod_db_hosts"]],
        safe_filters=[re.compile(p) for p in settings["safe_filters"]],
        extra_deny=[(re.compile(i["pattern"]), i["reason"]) for i in settings["extra_deny"]],
        tampering_allow_agents=settings["tampering_allow_agents"],
        git_write_allow_agents=settings["git_write_allow_agents"],
        warnings=warnings,
    )


@dataclass
class Token:
    text: str
    op: bool = False
    body: str = ""


def heredoc_delimiter(script: str, i: int) -> tuple[str, int]:
    while i < len(script) and script[i] in " \t":
        i += 1
    end = i
    while end < len(script) and script[end] not in " \t\n;&|()<>":
        end += 1
    return re.sub(r"[\\'\"]", "", script[i:end]), end


def skip_heredocs(script: str, i: int, delimiters: list[str]) -> int:
    for delimiter in delimiters:
        while i < len(script):
            end = script.find("\n", i)
            end = len(script) if end < 0 else end
            line = script[i:end]
            i = end + 1
            if line.lstrip("\t") == delimiter:
                break
    return i


def balanced(script: str, start: int) -> tuple[str, int, bool]:
    """The text of `$(…)` up to its closing parenthesis, the index after it, and if it closed."""
    depth, i, quote, heredocs = 1, start, "", []
    while i < len(script):
        char = script[i]
        if quote and char == "\\" and quote == '"':
            i += 2
            continue
        if quote:
            quote = "" if char == quote else quote
            i += 1
            continue
        if char == "\\":
            i += 2
            continue
        if script.startswith("<<", i) and not script.startswith("<<<", i):
            delimiter, i = heredoc_delimiter(script, i + (3 if script[i + 2 : i + 3] == "-" else 2))
            heredocs.append(delimiter)
            continue
        if char == "\n" and heredocs:
            i, heredocs = skip_heredocs(script, i + 1, heredocs), []
            continue
        if char == "#" and (i == start or script[i - 1] in " \t\n;&|("):
            end = script.find("\n", i)
            i = len(script) if end < 0 else end
            continue
        if char in "'\"":
            quote = char
        depth += (char == "(") - (char == ")")
        if depth == 0:
            return script[start:i], i + 1, True
        i += 1
    return script[start:], len(script), False


def closing(script: str, start: int, quote: str) -> int:
    i = start
    while i < len(script) and script[i] != quote:
        i += 2 if script[i] == "\\" else 1
    return min(i, len(script))


def decode_ansi(raw: str) -> str:
    try:
        return raw.encode("latin-1", "backslashreplace").decode("unicode_escape")
    except UnicodeError:
        return raw


class Lexer:
    def __init__(self, script: str) -> None:
        self.script = script
        self.i = 0
        self.tokens: list[Token] = []
        self.nested: list[str] = []
        self.word: list[str] = []
        self.in_word = False
        self.quoted = False
        self.heredocs: list[int] = []
        self.unclosed = False

    def run(self) -> list[Token]:
        script = self.script
        while self.i < len(script):
            char = script[self.i]
            if char == "\\":
                self.escape()
                continue
            if char == "'":
                end = script.find("'", self.i + 1)
                end = len(script) if end < 0 else end
                self.add(script[self.i + 1 : end], quoted=True)
                self.i = end + 1
                continue
            if char == '"':
                self.double()
                continue
            if script.startswith("$'", self.i):
                end = closing(script, self.i + 2, "'")
                self.add(decode_ansi(script[self.i + 2 : end]), quoted=True)
                self.i = end + 1
                continue
            if script.startswith("$(", self.i):
                inner, end, closed = balanced(script, self.i + 2)
                self.unclosed = self.unclosed or not closed
                self.nested.append(inner)
                self.add(script[self.i : end])
                self.i = end
                continue
            if char == "`":
                end = closing(script, self.i + 1, "`")
                self.nested.append(script[self.i + 1 : end])
                self.add(script[self.i : end + 1])
                self.i = end + 1
                continue
            if char in " \t\r":
                self.flush()
                self.i += 1
                continue
            if char == "#" and not self.in_word:
                end = script.find("\n", self.i)
                self.i = len(script) if end < 0 else end
                continue
            operator = next((op for op in OPERATORS if script.startswith(op, self.i)), None)
            if operator:
                self.operator(operator)
                continue
            self.add(char)
            self.i += 1
        self.flush()
        self.read_heredocs()
        return self.tokens

    def add(self, text: str, quoted: bool = False) -> None:
        self.word.append(text)
        self.in_word = True
        self.quoted = self.quoted or quoted

    def flush(self) -> None:
        if self.in_word:
            self.tokens.append(Token("".join(self.word)))
        self.word, self.in_word, self.quoted = [], False, False

    def escape(self) -> None:
        following = self.script[self.i + 1 : self.i + 2]
        self.i += 2
        if following != "\n":
            self.add(following, quoted=True)

    def double(self) -> None:
        script, i, out = self.script, self.i + 1, []
        while i < len(script) and script[i] != '"':
            if script[i] == "\\" and script[i + 1 : i + 2] in ('"', "\\", "$", "`", "\n"):
                out.append(script[i + 1].replace("\n", ""))
                i += 2
                continue
            if script.startswith("$(", i):
                inner, end, closed = balanced(script, i + 2)
                self.unclosed = self.unclosed or not closed
                self.nested.append(inner)
                out.append(script[i:end])
                i = end
                continue
            if script[i] == "`":
                end = closing(script, i + 1, "`")
                self.nested.append(script[i + 1 : end])
                out.append(script[i : end + 1])
                i = end + 1
                continue
            out.append(script[i])
            i += 1
        self.add("".join(out), quoted=True)
        self.i = i + 1

    def operator(self, operator: str) -> None:
        if operator[0] in "<>" and not self.quoted and "".join(self.word).isdigit():
            self.word, self.in_word = [], False
        self.flush()
        self.i += len(operator)
        if operator in ("<<", "<<-"):
            self.heredocs.append(len(self.tokens))
        self.tokens.append(Token(operator, op=True))
        if operator == "\n":
            self.read_heredocs()

    def read_heredocs(self) -> None:
        pending, self.heredocs = self.heredocs, []
        for index in pending:
            delimiter = self.tokens[index + 1] if index + 1 < len(self.tokens) else None
            if delimiter is None or delimiter.op:
                continue
            lines = []
            while self.i < len(self.script):
                end = self.script.find("\n", self.i)
                end = len(self.script) if end < 0 else end
                line = self.script[self.i : end]
                self.i = end + 1
                if line.lstrip("\t") == delimiter.text:
                    break
                lines.append(line)
            self.tokens[index].body = "\n".join(lines)


@dataclass
class Command:
    words: list[str] = field(default_factory=list)
    redirects: list[tuple[str, str]] = field(default_factory=list)
    stdin: list[str] = field(default_factory=list)
    after: str = ""


def commands(tokens: list[Token]) -> list[Command | str]:
    items: list[Command | str] = []
    current = Command()
    i = 0
    while i < len(tokens):
        token = tokens[i]
        i += 1
        if not token.op:
            current.words.append(token.text)
            continue
        if token.text in REDIRECTS:
            has_target = i < len(tokens) and not tokens[i].op
            target = tokens[i].text if has_target else ""
            i += 1 if has_target else 0
            current.redirects.append((token.text, target))
            if token.text in ("<<", "<<-"):
                current.stdin.append(token.body)
            if token.text == "<<<":
                current.stdin.append(target)
            continue
        current.after = token.text
        if current.words or current.redirects:
            items.append(current)
        if token.text in ("(", ")"):
            items.append(token.text)
        current = Command()
    if current.words or current.redirects:
        items.append(current)
    return items


@dataclass
class Call:
    program: str
    args: list[str]
    env: dict[str, str]
    via_xargs: bool


def skip_options(words: list[str], i: int, with_value: set[str]) -> int:
    while i < len(words) and words[i].startswith("-"):
        if words[i] == "--":
            return i + 1
        i += 2 if words[i] in with_value else 1
    return i


def unwrap(words: list[str]) -> Call | None:
    env: dict[str, str] = {}
    via_xargs = False
    i = 0
    while i < len(words):
        word = words[i]
        if ASSIGNMENT.match(word):
            name, _, value = word.partition("=")
            env[name] = value
            i += 1
            continue
        if word in RESERVED:
            i += 1
            continue
        program = PurePath(word).name if word else word
        if program not in WRAPPERS:
            return Call(program, words[i + 1 :], env, via_xargs)
        i = skip_options(words, i + 1, WRAPPERS[program])
        i += 1 if program == "timeout" else 0
        via_xargs = via_xargs or program == "xargs"
    return None


def parse(
    args: list[str],
    short_values: str,
    long_values: set[str],
    values: dict[str, list[str]] | None = None,
) -> tuple[set[str], list[str]]:
    """The flags and the operands; the option values go into `values` when it is given."""
    flags: set[str] = set()
    operands: list[str] = []
    found: dict[str, list[str]] = {} if values is None else values
    i = 0
    while i < len(args):
        arg = args[i]
        i += 1
        if arg == "--":
            operands += args[i:]
            break
        if arg.startswith("--"):
            name, equals, value = arg.partition("=")
            flags.add(name)
            if name in long_values and not equals:
                value = args[i] if i < len(args) else ""
                i += 1
            found.setdefault(name, []).append(value)
            continue
        if not arg.startswith("-") or arg == "-":
            operands.append(arg)
            continue
        for position, letter in enumerate(arg[1:], start=2):
            flags.add("-" + letter)
            if letter in short_values:
                attached = arg[position:]
                found.setdefault("-" + letter, []).append(
                    attached or (args[i] if i < len(args) else "")
                )
                i += 1 if position == len(arg) else 0
                break
    return flags, operands


def short_flag(arg: str, letter: str, values: str = "") -> bool:
    if not arg.startswith("-") or arg.startswith("--"):
        return False
    for char in arg[1:]:
        if char == letter:
            return True
        if char in values:
            return False
    return False


def reader_files(program: str, args: list[str]) -> list[str] | None:
    if program in GREPS:
        flags, operands = parse(args, "efmABCdD", GREP_LONG_VALUES)
        if flags & GREP_QUIET:
            return None
        return operands if flags & {"-e", "-f", "--regexp", "--file"} else operands[1:]
    if program == "sed":
        flags, operands = parse(args, "efl", {"--expression", "--file", "--line-length"})
        if flags & {"-i", "--in-place"}:
            return None
        return operands if flags & {"-e", "-f", "--expression", "--file"} else operands[1:]
    if program in ("awk", "gawk"):
        flags, operands = parse(args, "fvF", {"--file", "--assign", "--field-separator"})
        return operands if flags & {"-f", "--file"} else operands[1:]
    if program == "rg":
        flags, operands = parse(args, "efgtTmABCMjErd", RG_LONG_VALUES)
        if flags & RG_QUIET:
            return None
        return operands if flags & {"-e", "-f", "--regexp", "--file"} else operands[1:]
    if program == "jq":
        return jq_files(args)
    if program == "wc":
        return name_lists(args)
    if program == "cut" and names_only(args):
        return None
    operands = [arg for arg in args if not arg.startswith("-")]
    return operands + name_lists(args) if program == "sort" else operands


def jq_files(args: list[str]) -> list[str]:
    files, operands, i = [], [], 0
    while i < len(args):
        arg = args[i]
        if arg in JQ_PAIRS:
            files += args[i + 2 : i + 3] if arg in ("--slurpfile", "--rawfile") else []
            i += 3
            continue
        i += 1
        if arg in ("-f", "--from-file"):
            operands.append("")
            continue
        if not arg.startswith("-") or arg == "-":
            operands.append(arg)
    return files + [operand for operand in operands[1:] if operand]


def find_roots(args: list[str]) -> list[str]:
    i = 0
    while i < len(args) and (args[i] in ("-H", "-L", "-P", "-D") or args[i].startswith("-O")):
        i += 2 if args[i] == "-D" else 1
    roots = []
    for arg in args[i:]:
        if arg.startswith("-") or arg in ("(", "!"):
            break
        roots.append(arg)
    return roots or ["."]


def find_deletes(args: list[str]) -> bool:
    for i, arg in enumerate(args):
        if arg == "-delete":
            return True
        runs = arg in ("-exec", "-execdir", "-ok", "-okdir") and i + 1 < len(args)
        if runs and PurePath(args[i + 1]).name == "rm":
            return True
    return False


def shell_script(args: list[str]) -> tuple[bool, str | None]:
    has_c = False
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in ("-o", "+o", "-O", "+O", "--rcfile", "--init-file"):
            i += 2
            continue
        if arg.startswith(("-", "+")):
            has_c = has_c or (not arg.startswith("--") and "c" in arg)
            i += 1
            continue
        return has_c, arg
    return has_c, None


def bare_env(words: list[str]) -> bool:
    """Whether the words run `env` without a command, which prints every variable."""
    i, found = 0, False
    while i < len(words):
        if ASSIGNMENT.match(words[i]) or words[i] in RESERVED:
            i += 1
            continue
        program = PurePath(words[i]).name
        if program not in WRAPPERS:
            return False
        found = found or program == "env"
        i = skip_options(words, i + 1, WRAPPERS[program])
        i += 1 if program == "timeout" else 0
    return found


def names_only(args: list[str]) -> bool:
    """Whether `cut` prints only what comes before the first `=`, and only of lines with one."""
    values: dict[str, list[str]] = {}
    flags, _ = parse(args, "bcdf", CUT_VALUES, values)
    delimiters = values.get("-d", []) + values.get("--delimiter", [])
    fields = values.get("-f", []) + values.get("--fields", [])
    delimited = bool(flags & {"-s", "--only-delimited"}) and flags <= CUT_NAMES_ONLY
    return delimited and set(delimiters) == {"="} and set(fields) == {"1"}


def reads_names(arg: str) -> bool:
    """Whether an argument is `--files0-from`, which GNU accepts abbreviated."""
    name = arg.split("=", 1)[0]
    return len(name) > 2 and "--files0-from".startswith(name)


def name_lists(args: list[str]) -> list[str]:
    """The files `--files0-from` reads file names from; an error prints each name it reads."""
    files = []
    for i, arg in enumerate(args):
        _, equals, value = arg.partition("=")
        if reads_names(arg):
            files.append(value if equals else "".join(args[i + 1 : i + 2]))
    return files


def copy_paths(program: str, args: list[str]) -> tuple[list[str], list[str]]:
    """The sources and the destinations of a copy, move or link."""
    if program == "dd":
        sources = [arg[3:] for arg in args if arg.startswith("if=")]
        return sources, [arg[3:] for arg in args if arg.startswith("of=")]
    short_values, long_values = COPIERS[program]
    values: dict[str, list[str]] = {}
    _, operands = parse(args, short_values, long_values, values)
    targets = values.get("-t", []) + values.get("--target-directory", [])
    if "t" not in short_values or not targets:
        return operands[:-1], operands[-1:]
    return operands, targets


def changed_paths(call: Call) -> list[str]:
    """The paths a command writes, replaces, moves away or deletes, as far as its words tell."""
    name, args = call.program, call.args
    in_place = name == "sed" and any(
        arg.startswith("--in-place") or short_flag(arg, "i", "efl") for arg in args
    )
    in_place = in_place or (name == "perl" and any(short_flag(a, "i", "eEIMmFdD") for a in args))
    if name in CHANGERS or in_place:
        return [arg for arg in args if not arg.startswith("-")]
    if name != "dd" and name not in COPIERS:
        return []
    sources, destinations = copy_paths(name, args)
    into = [str(PurePath(d, PurePath(s).name)) for d in destinations for s in sources]
    return (sources if name == "mv" else []) + destinations + into


def force_deletes(args: list[str]) -> bool:
    if any(short_flag(arg, "D", "u") for arg in args):
        return True
    deletes = "--delete" in args or any(short_flag(arg, "d", "u") for arg in args)
    return deletes and ("--force" in args or any(short_flag(arg, "f", "u") for arg in args))


def tag_writes(args: list[str]) -> bool:
    """`git tag` that names a tag to create or delete; listing and verifying only read."""
    flags, operands = parse(args, "mFu", TAG_VALUES)
    return bool(operands) and not flags & TAG_LISTS


def asks_help(args: list[str]) -> bool:
    return "--help" in args or "-h" in args


def before_paths(args: list[str]) -> list[str]:
    if "--" not in args:
        return args
    return args[: args.index("--")]


def moves_branch(sub: str, args: list[str], is_path: Callable[[str], bool]) -> bool:
    """`branch -f`/`-M`/`-C`, `checkout -B`, `switch -C` and `reset <commit>` (with `--soft`
    too): they move or overwrite a branch without a commit; `reset --hard` is a discard, and
    `reset <path>` only unstages."""
    if sub == "branch":
        forced = "--force" in args or any(short_flag(a, f, "u") for a in args for f in "fMC")
        return forced and not force_deletes(args)
    if sub == "checkout":
        return any(short_flag(arg, "B", "b") for arg in args)
    if sub == "switch":
        return "--force-create" in args or any(short_flag(arg, "C", "c") for arg in args)
    if sub != "reset" or "--hard" in args:
        return False
    _, operands = parse(before_paths(args), "", RESET_VALUES)
    if not operands or operands[0] in ("HEAD", "@", "."):
        return False
    return not is_path(operands[0])


def fetch_writes(args: list[str]) -> bool:
    """A refspec that forces (`+`) or lands outside `refs/remotes/`, so it moves a local ref."""
    _, operands = parse(args, "jo", FETCH_VALUES)
    for refspec in operands[1:]:
        destination = refspec.partition(":")[2]
        if refspec.startswith("+") or (destination and not destination.startswith("refs/remotes/")):
            return True
    return False


def subtree_writes(args: list[str]) -> bool:
    flags, operands = parse(args, "Pmb", SUBTREE_VALUES)
    command = (operands or [""])[0]
    return command in SUBTREE_WRITES or (command == "split" and "--rejoin" in flags)


def writes_history(sub: str, args: list[str], is_path: Callable[[str], bool]) -> bool:
    if asks_help(args) or (sub in ABORTABLE and "--abort" in args):
        return False
    if "--dry-run" in args and sub in ("commit", "push"):
        return False
    if sub == "push" and any(short_flag(arg, "n", "o") for arg in args):
        return False
    if sub == "tag":
        return tag_writes(args)
    if sub == "fetch":
        return fetch_writes(args)
    if sub == "subtree":
        return subtree_writes(args)
    return sub in GIT_WRITES or moves_branch(sub, args, is_path)


def push_path(args: list[str]) -> str | None:
    """The destination of `git push` when it is a local path; a remote name or URL is not."""
    values: dict[str, list[str]] = {}
    _, operands = parse(args, "o", PUSH_VALUES, values)
    target = (values.get("--repo") or operands or [""])[0]
    if target.startswith("file://"):
        return target.removeprefix("file://")
    if target.startswith(("/", "./", "../")):
        return target
    return None


def forge_command(args: list[str]) -> tuple[str, str, list[str]]:
    """The command group, its action and the arguments after them, past `-R`/`--repo`."""
    i = skip_options(args, 0, FORGE_VALUES)
    group = (args[i:] or [""])[0]
    j = skip_options(args, i + 1, FORGE_VALUES)
    action = (args[j:] or [""])[0]
    return group, action, args[i + 1 :]


def api_writes(args: list[str]) -> bool:
    """`gh api`/`glab api` with a method other than GET or HEAD, or fields (an implicit POST)."""
    values: dict[str, list[str]] = {}
    flags, _ = parse(args, "XHfFqtp", API_VALUES, values)
    methods = values.get("-X", []) + values.get("--method", [])
    if methods:
        return any(method.upper() not in ("GET", "HEAD") for method in methods)
    return bool(flags & API_FIELDS)


def sets_hooks_path(args: list[str]) -> bool:
    keys = [i for i, arg in enumerate(args) if arg.lower() == "core.hookspath"]
    if not keys or CONFIG_READS & set(args):
        return False
    value = any(not arg.startswith("-") for arg in args[keys[0] + 1 :])
    return value or bool(CONFIG_CHANGES & set(args))


def claude_config_dir(environ: Mapping[str, str]) -> str:
    home = environ.get("HOME") or str(Path.home())
    config = expand(environ.get("CLAUDE_CONFIG_DIR", "").strip(), environ)
    return config or str(PurePath(home, ".claude"))


def live_guards(environ: Mapping[str, str]) -> list[str]:
    """Globs of guard files that are live wherever they are: user and harness settings, plugins."""
    config = claude_config_dir(environ)
    files = [(str(harness_home(environ)), name) for name in ("guard.json", "memory.json")]
    files += [(config, name) for name in ("settings.json", "plugins", "plugins/*")]
    globs = [str(PurePath(glob.escape(os.path.realpath(d)), name)).lower() for d, name in files]
    return globs + bridge_guards(environ)


def bridge_guards(environ: Mapping[str, str]) -> list[str]:
    """Globs of the permission bridge's folder, where a decision file answers a prompt."""
    folder = PurePath(glob.escape(os.path.realpath(harness_home(environ))), "permissions")
    return [str(folder).lower(), str(folder / "*").lower()]


def user_settings(environ: Mapping[str, str]) -> list[str]:
    """Globs of the user's Claude Code settings.json: as named, in the resolved dir, resolved."""
    path = os.path.normpath(PurePath(claude_config_dir(environ), "settings.json"))
    forms = [path, resolved_dir(path), os.path.realpath(path)]
    return [glob.escape(form).lower() for form in dict.fromkeys(forms)]


def resolved_dir(path: str) -> str:
    """The path with its directory resolved and its last part kept, a link or not."""
    pure = PurePath(path)
    return str(PurePath(os.path.realpath(pure.parent), pure.name))


def db_hosts(call: Call) -> list[str]:
    hosts = [value for name, value in call.env.items() if name in DB_HOST_ENV]
    args = call.args
    for i, arg in enumerate(args):
        if arg in ("-h", "--host") and i + 1 < len(args):
            hosts.append(args[i + 1])
        if arg.startswith("--host="):
            hosts.append(arg[len("--host=") :])
        if arg.startswith("-h") and not arg.startswith("--") and len(arg) > 2:
            hosts.append(arg[2:])
        if "host=" in arg and not arg.startswith("--"):
            hosts.append(arg)
        if "://" in arg:
            hosts.append(uri_host(arg))
    return hosts


def uri_host(uri: str) -> str:
    try:
        return urlsplit(uri).hostname or uri
    except ValueError:
        return uri


def capped(dirs: list[str | None]) -> list[str | None]:
    """The possible working directories; past MAX_DIRS of them, one unknown directory."""
    unique = list(dict.fromkeys(dirs))
    return unique if len(unique) <= MAX_DIRS else [None]


@dataclass
class Denial:
    rule: str
    what: str
    proceed: str


class Checker:
    def __init__(
        self,
        config: Config,
        environ: Mapping[str, str],
        cwd: str | None,
        subagent: bool,
        project: str | None = None,
        may_change_user_settings: bool = False,
        may_write_git: bool = False,
        bridge_only: bool = False,
    ) -> None:
        self.config = config
        self.environ = environ
        start = os.path.realpath(cwd) if cwd else None
        self.cwds: list[str | None] = [start]
        self.history: list[str | None] = [start]
        self.stack: list[tuple[list[str | None], list[str | None]]] = []
        self.subagent = subagent
        self.project = os.path.realpath(project) if project else None
        self.bridge_only = bridge_only
        self.live = bridge_guards(environ) if bridge_only else live_guards(environ)
        self.repo_guards = [] if bridge_only else REPO_GUARDS
        self.own_settings = user_settings(environ) if may_change_user_settings else []
        self.git_writes = subagent and not may_write_git

    def script(self, text: str, depth: int = 0) -> Denial | None:
        if depth > MAX_DEPTH and self.bridge_only:
            return None
        if depth > MAX_DEPTH:
            return Denial(
                "nesting",
                f"the command nests shells more than {MAX_DEPTH} levels deep, too deep to check",
                "run the inner command directly.",
            )
        lexer = Lexer(text)
        items = commands(lexer.run())
        if lexer.unclosed and not self.bridge_only:
            return Denial(
                "unparsable",
                "a `$(` in the command is never closed, so the rest of it cannot be checked",
                "close the command substitution; for a multi-line message, write it to a file "
                "and pass the file (`git commit -F msg.txt`).",
            )
        for index in range(len(items)):
            denial = self.item(items, index, depth)
            if denial:
                return denial
        for inner in lexer.nested:
            denial = self.subshell(inner, depth + 1)
            if denial:
                return denial
        return None

    def subshell(self, text: str, depth: int) -> Denial | None:
        saved = (list(self.cwds), list(self.history))
        denial = self.script(text, depth)
        self.cwds, self.history = saved
        return denial

    def item(self, items: list[Command | str], index: int, depth: int) -> Denial | None:
        item = items[index]
        if item == "(":
            self.stack.append((list(self.cwds), list(self.history)))
            return None
        if isinstance(item, str):
            if self.stack:
                self.cwds, self.history = self.stack.pop()
            return None
        call = unwrap(item.words)
        denial = self.redirects(call, item, items, index)
        if denial is None and call is None and bare_env(item.words) and not self.bridge_only:
            denial = self.environment(" ".join(item.words), items, index)
        if denial is None and call is not None:
            denial = self.call(call, item, items, index, depth)
        if item.after == "||":
            self.cwds = capped(self.history)
        return denial

    def call(
        self, call: Call, command: Command, items: list[Command | str], index: int, depth: int
    ) -> Denial | None:
        name = call.program
        if name in ("cd", "pushd", "popd"):
            self.change_dir(call, command.after)
            return None
        if name in SHELLS:
            return self.shell(call, command, depth)
        if name == "eval":
            return self.subshell(" ".join(call.args), depth + 1)
        changed = self.tamper_denial(f"`{name}`", changed_paths(call))
        if changed or self.bridge_only:
            return changed
        if name == "rm":
            return self.rm(call, items, index)
        if name == "find" and find_deletes(call.args):
            return self.find(call)
        if name == "git":
            return self.git(call, items, index)
        if name in FORGE_WRITES:
            return self.forge(call)
        if name in READERS:
            return self.secret_read(call, command, items, index)
        if name in SECRET_COPIERS:
            return self.secret_copy(call)
        if name in DB_CLIENTS:
            return self.database(call, command, items, index)
        if name == "security":
            return self.keychain(call)
        return self.printed_env(call, items, index)

    def redirects(
        self, call: Call | None, command: Command, items: list[Command | str], index: int
    ) -> Denial | None:
        written = [target for op, target in command.redirects if op in WRITES]
        denial = self.tamper_denial("a `>` redirect", written)
        if denial is not None or self.bridge_only:
            return denial
        if call is not None and call.program in READERS:
            return None
        read = [target for op, target in command.redirects if op in ("<", "<>")]
        secret = next((file for file in read if self.is_secret(file)), None)
        if secret is None or self.safe_pipe(items, index) or self.safe_redirect(call, command):
            return None
        program = f"`{call.program}`" if call else "the command"
        return Denial(
            "secret-read",
            f"`< {secret}` hands the file to {program}, which can print it, and the file "
            "matches secret_globs",
            PRINT_PROCEED,
        )

    def tamper_denial(self, how: str, paths: list[str]) -> Denial | None:
        target = next((path for path in paths if self.guarded(path)), None)
        if target is None:
            return None
        return Denial(
            "guard-tampering",
            f"{how} would change {target}, which holds Claude Code settings, the guard's "
            "configuration, a git hook, an installed plugin or a permission request",
            TAMPER_PROCEED,
        )

    def guarded(self, target: str) -> bool:
        resolved = [self.path(target, cwd) for cwd in self.cwds]
        known = [path for path in resolved if path is not None]
        lexical = [p for p in (self.path(target, cwd, resolve=False) for cwd in self.cwds) if p]
        candidates = [path.lower() for path in known + lexical]
        if len(known) < len(resolved):
            candidates.append(expand(target, self.environ).lower())
        own = self.own_settings
        candidates = [c for c in candidates if not any(fnmatchcase(c, p) for p in own)]
        if any(fnmatchcase(c, pattern) for c in candidates for pattern in self.live):
            return True
        repo = self.repo_guards
        if not any(fnmatchcase("/" + c, pattern) for c in candidates for pattern in repo):
            return False
        # A link is live where it lies, not where it points; its dir may be a link (/tmp on macOS).
        links = [resolved_dir(path) for path in lexical]
        return len(known) < len(resolved) or not all(self.disposable(p) for p in known + links)

    def disposable(self, path: str) -> bool:
        """Inside the temp dir or scratchpad, and not the session's project."""
        parents = [str(parent) for parent in PurePath(path).parents]
        temp = any(fnmatchcase(p, pattern) for p in parents for pattern in self.config.rm_dirs)
        return temp and not (self.project and PurePath(path).is_relative_to(self.project))

    def path(self, text: str, cwd: str | None, resolve: bool = True) -> str | None:
        text = expand(text, self.environ)
        if not text or "$" in text or "`" in text or text.startswith("~"):
            return None
        candidate = PurePath(text)
        if not candidate.is_absolute() and cwd is None:
            return None
        if not candidate.is_absolute():
            candidate = PurePath(cwd or "") / candidate
        return os.path.realpath(candidate) if resolve else os.path.normpath(candidate)

    def inside(self, path: str | None) -> bool:
        if path is None:
            return False
        parents = [str(parent) for parent in PurePath(path).parents]
        if any(fnmatchcase(p, pattern) for p in parents for pattern in self.config.rm_dirs):
            return True
        return any(
            fnmatchcase(p, pattern) for p in [path, *parents] for pattern in self.config.rm_trees
        )

    def allowed(self, target: str, cwds: list[str | None] | None = None) -> bool:
        return all(self.inside(self.path(target, cwd)) for cwd in cwds or self.cwds)

    def change_dir(self, call: Call, after: str) -> None:
        operands = [arg for arg in call.args if not arg.startswith("-")]
        target = operands[0] if operands else "~"
        known = call.program != "popd" and "-" not in call.args
        new = [self.path(target, cwd) if known else None for cwd in self.cwds]
        self.history = capped(self.history + new)
        if after == "&&":
            self.cwds = capped(new)
            return
        self.cwds = capped(self.cwds + new)

    def shell(self, call: Call, command: Command, depth: int) -> Denial | None:
        has_c, operand = shell_script(call.args)
        if has_c and operand is not None:
            return self.subshell(operand, depth + 1)
        if not has_c and operand is None and command.stdin:
            return self.subshell("\n".join(command.stdin), depth + 1)
        return None

    def rm(self, call: Call, items: list[Command | str], index: int) -> Denial | None:
        recursive, ended, targets = False, False, []
        for arg in call.args:
            if ended or not arg.startswith("-") or arg == "-":
                targets.append(arg)
                continue
            ended = arg == "--"
            short = not arg.startswith("--") and ("r" in arg or "R" in arg)
            recursive = recursive or short or arg == "--recursive"
        if not recursive:
            return None
        if call.via_xargs and not self.from_allowed_find(items, index):
            return Denial(
                "rm-outside-allowed-dirs",
                "`xargs rm -r` takes its paths from stdin, so they cannot be checked",
                f"run `rm -r` on literal paths inside {self.allowed_text()}, or feed it from a "
                "`find` whose start paths are there.",
            )
        outside = [target for target in targets if not self.allowed(target)]
        if not outside:
            return None
        return self.rm_denial("`rm -r`", outside)

    def from_allowed_find(self, items: list[Command | str], index: int) -> bool:
        previous = items[index - 1] if index > 0 else None
        if not isinstance(previous, Command) or previous.after not in PIPES:
            return False
        call = unwrap(previous.words)
        if call is None or call.program != "find":
            return False
        return all(self.allowed(root) for root in find_roots(call.args))

    def find(self, call: Call) -> Denial | None:
        outside = [root for root in find_roots(call.args) if not self.allowed(root)]
        if not outside:
            return None
        return self.rm_denial("`find -delete`/`-exec rm`", outside)

    def allowed_text(self) -> str:
        return "the temp dir, the Claude scratchpad or allowed_rm_dirs"

    def rm_denial(self, what: str, outside: list[str]) -> Denial:
        return Denial(
            "rm-outside-allowed-dirs",
            f"{what} on {', '.join(outside)}, which is not inside {self.allowed_text()} "
            "(a path with a variable or in an unknown directory counts as outside)",
            "delete only literal paths in those dirs, add the dir to `allowed_rm_dirs` under "
            "`guard` in the project's .claude/agent-harness.json, or ask the person to delete it.",
        )

    def git(self, call: Call, items: list[Command | str], index: int) -> Denial | None:
        dirs = list(self.cwds)
        work = list(self.cwds)
        git_dir = call.env.get("GIT_DIR")
        args = call.args
        i = 0
        while i < len(args) and args[i].startswith("-"):
            arg = args[i]
            name, _, value = arg.partition("=")
            if not value and name == arg and arg in GIT_VALUE_OPTIONS and i + 1 < len(args):
                value = args[i + 1]
            if value and name in ("-C", "--work-tree"):
                dirs = [self.path(value, d) for d in dirs]
            if value and name == "--git-dir":
                git_dir = value
            if value and name == "-C":
                work = [self.path(value, d) for d in work]
            i += 2 if arg in GIT_VALUE_OPTIONS else 1
        if i >= len(args):
            return None
        repos = None
        if git_dir is not None:
            repos = [self.path(git_dir, d) for d in work]
        sub, rest = args[i], args[i + 1 :]
        if any("core.hookspath" in arg.lower() for arg in args[:i]) or (
            sub == "config" and sets_hooks_path(rest)
        ):
            return Denial(
                "guard-tampering",
                "`git config core.hooksPath` (or `git -c core.hooksPath=…`) points git at other "
                "hooks than the repository's",
                "leave it to the person and put the change you need in your report.",
            )
        if sub == "push" and (forced := self.push(rest)) is not None:
            return forced
        if writes_history(sub, rest, lambda arg: self.existing(arg, work)):
            return self.git_write(sub, rest, work, repos)
        if sub in ("show", "log"):
            return self.git_secret(sub, rest, items, index)
        scratch = self.scratch(work, repos)
        if sub == "branch" and force_deletes(rest) and not scratch:
            return Denial(
                "branch-force-delete",
                "`git branch -D` (or `--delete --force`) deletes a branch even when its commits "
                "are on no other branch",
                "delete it with `git branch -d`, which refuses an unmerged branch, or ask the "
                "person. It is allowed in a repository inside the temp dir or scratchpad.",
            )
        discard = self.discards(sub, rest, dirs)
        if discard is None or (scratch and all(self.temp(d) for d in dirs)):
            return None
        return Denial(
            "git-discard",
            f"`git {discard}` throws away uncommitted work and cannot be undone",
            "keep the work with `git stash` or a commit, discard single files by name, or ask "
            "the person. It is allowed only in a repository inside the temp dir or scratchpad.",
        )

    def git_write(
        self, sub: str, rest: list[str], work: list[str | None], repos: list[str | None] | None
    ) -> Denial | None:
        """Commits and the like in a repository inside the temp dirs pass, and pushes to a path
        there; the rest is the team lead's. `--git-dir` or `GIT_DIR` names the repository when
        given, otherwise the `.git` above the working directory does (`--work-tree` does not)."""
        if not self.git_writes:
            return None
        if sub != "push" and self.scratch(work, repos):
            return None
        target = None
        if sub == "push":
            target = push_path(rest)
        if target is not None and all(self.temp(self.path(target, d)) for d in work):
            return None
        return Denial(
            "git-write",
            f"`git {sub}` writes the repository's history or the remote, which is the team "
            "lead's to do after the person approves",
            GIT_WRITE_PROCEED,
        )

    def temp(self, path: str | None) -> bool:
        """Below the temp dir or scratchpad; unlike `inside`, not the project's build dirs."""
        if path is None:
            return False
        parents = [str(parent) for parent in PurePath(path).parents]
        return any(fnmatchcase(p, pattern) for p in parents for pattern in self.config.rm_dirs)

    def existing(self, arg: str, work: list[str | None]) -> bool:
        paths = [self.path(arg, d) for d in work]
        return all(path is not None and Path(path).exists() for path in paths)

    def scratch(self, work: list[str | None], repos: list[str | None] | None) -> bool:
        """A repository inside the temp dirs: the one `--git-dir` or `GIT_DIR` names, otherwise
        the one git finds above the working directory."""
        if repos is not None:
            return all(self.scratch_git_dir(d) for d in repos)
        return all(self.scratch_repo(d) for d in work)

    def scratch_git_dir(self, path: str | None) -> bool:
        """A git dir inside the temp dirs, links resolved; a `.git` file counts by the `gitdir:`
        it points to, and one that cannot be read counts as outside."""
        if path is not None:
            path = os.path.realpath(path)
        if path is None or not self.temp(path):
            return False
        try:
            if not Path(path).is_file():
                return True
            text = Path(path).read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return False
        if not text.startswith("gitdir:"):
            return False
        target = self.path(text.removeprefix("gitdir:").strip(), str(Path(path).parent))
        return target is not None and self.temp(target)

    def scratch_repo(self, path: str | None) -> bool:
        """A working directory inside the temp dirs whose repository, the first `.git` above it
        up to `/`, is there too; without a `.git` git fails anyway."""
        if path is None or not self.temp(path):
            return False
        for parent in [path, *map(str, PurePath(path).parents)]:
            dot_git = Path(parent, ".git")
            if dot_git.is_symlink() or dot_git.exists():
                return self.scratch_git_dir(str(dot_git))
        return True

    def forge(self, call: Call) -> Denial | None:
        group, action, rest = forge_command(call.args)
        what = f"`{call.program} {group} {action}`"
        writes = action in FORGE_WRITES[call.program].get(group, set())
        if call.program == "gh" and group == "pr" and action == "review":
            verdicts = {"--approve", "--request-changes"}
            writes = bool(verdicts & set(rest)) or any(
                short_flag(arg, letter, "bF") for arg in rest for letter in "ar"
            )
        if group == "api":
            what, writes = f"`{call.program} api` with a write method or fields", api_writes(rest)
        if not writes or not self.git_writes or asks_help(rest):
            return None
        return Denial(
            "git-write",
            f"{what} writes to the forge (a merge request, release, repository or API "
            "resource), which is the team lead's to do after the person approves",
            GIT_WRITE_PROCEED,
        )

    def push(self, rest: list[str]) -> Denial | None:
        force = any(
            arg == "--force" or arg.startswith("+") or short_flag(arg, "f", "o") for arg in rest
        )
        if force:
            return Denial(
                "force-push",
                "`git push --force` (or `-f`, `+refspec`) overwrites the remote branch",
                "push without force. If the history really must be rewritten, the person runs "
                "the force-push; a subagent puts it in its report.",
            )
        deletes = any(
            arg in ("--delete", "--mirror", "--prune")
            or short_flag(arg, "d", "o")
            or (arg.startswith(":") and len(arg) > 1)
            for arg in rest
        )
        if deletes:
            return Denial(
                "push-delete",
                "`git push --delete` (or `-d`, `:ref`, `--prune`, `--mirror`) deletes branches or "
                "tags on the remote, and `--mirror` also overwrites every remote branch",
                "push the branch without deleting anything. Deleting remote refs is for the "
                "person to do; a subagent puts it in its report.",
            )
        lease = any(a == "--force-with-lease" or a.startswith("--force-with-lease=") for a in rest)
        setting = self.config.allow_force_with_lease
        if not lease or setting is True or (setting == "main" and not self.subagent):
            return None
        return Denial(
            "force-with-lease",
            "`git push --force-with-lease` still rewrites the remote branch, and "
            "allow_force_with_lease in the guard config does not allow it here",
            "push without force, or put the push in your report so the team lead runs it "
            "after the person approves.",
        )

    def discards(self, sub: str, rest: list[str], dirs: list[str | None]) -> str | None:
        if sub == "reset" and "--hard" in rest:
            return "reset --hard"
        if sub == "checkout" and ("--force" in rest or any(short_flag(a, "f", "bB") for a in rest)):
            return "checkout --force"
        if sub == "clean":
            force = "--force" in rest or any(short_flag(a, "f", "e") for a in rest)
            dry = "--dry-run" in rest or any(short_flag(a, "n", "e") for a in rest)
            return "clean -f" if force and not dry else None
        if sub not in ("checkout", "restore"):
            return None
        whole = [arg for arg in rest if self.whole_tree(arg, dirs)]
        if not whole:
            return None
        staged = "--staged" in rest or any(short_flag(a, "S", "s") for a in rest)
        worktree = "--worktree" in rest or any(short_flag(a, "W", "s") for a in rest)
        if sub == "restore" and staged and not worktree:
            return None
        return f"{sub} {whole[0]}"

    def whole_tree(self, arg: str, dirs: list[str | None]) -> bool:
        if arg in WHOLE_TREE:
            return True
        if arg.startswith("-"):
            return False
        for d in dirs:
            path = self.path(arg, d)
            if d is not None and path is not None and PurePath(d).is_relative_to(path):
                return True
        return False

    def secret_read(
        self, call: Call, command: Command, items: list[Command | str], index: int
    ) -> Denial | None:
        files = reader_files(call.program, call.args)
        if files is None:
            return None
        if not self.safe_redirect(call, command):
            files += [target for op, target in command.redirects if op == "<"]
        return self.secret_denial(call.program, files, items, index)

    def git_secret(
        self, sub: str, rest: list[str], items: list[Command | str], index: int
    ) -> Denial | None:
        dashes = rest.index("--") if "--" in rest else len(rest)
        paths = rest[dashes + 1 :]
        if sub == "show":
            paths += [arg.split(":", 1)[1] for arg in rest[:dashes] if ":" in arg]
        if sub == "log" and not {"-p", "-u", "--patch"} & set(rest):
            return None
        if sub == "log":
            paths += [arg for arg in rest[:dashes] if not arg.startswith("-")]
        return self.secret_denial(f"git {sub}", paths, items, index)

    def secret_denial(
        self, program: str, files: list[str], items: list[Command | str], index: int
    ) -> Denial | None:
        secret = next((file for file in files if self.is_secret(file)), None)
        if secret is None or self.safe_pipe(items, index):
            return None
        return Denial(
            "secret-read",
            f"`{program}` would print {secret} raw into the transcript, and the file "
            "matches secret_globs",
            PRINT_PROCEED,
        )

    def secret_copy(self, call: Call) -> Denial | None:
        sources, _ = copy_paths(call.program, call.args)
        secret = next((file for file in sources if self.is_secret(file)), None)
        if secret is None:
            return None
        return Denial(
            "secret-copy",
            f"`{call.program}` would copy {secret}, which matches secret_globs, where the "
            "guard cannot follow it",
            "leave the file where it is: start a new one from the example "
            "(`cp .env.example .env`), or ask the person to copy it.",
        )

    def printed_env(self, call: Call, items: list[Command | str], index: int) -> Denial | None:
        operands = [arg for arg in call.args if not arg.startswith("-")]
        printenv = call.program == "printenv" and (
            not operands or any(SECRET_NAME.search(name) for name in operands)
        )
        export = call.program == "export" and set(call.args) <= {"-p"}
        if not printenv and not export:
            return None
        return self.environment(" ".join([call.program, *call.args]), items, index)

    def environment(self, text: str, items: list[Command | str], index: int) -> Denial | None:
        if self.safe_pipe(items, index):
            return None
        return Denial(
            "env-dump",
            f"`{text}` prints environment variables with their values, which hold tokens "
            "and passwords",
            "list the names with `env | cut -s -d= -f1`, or check that a variable is set without "
            'printing it (`test -n "$NAME"`, `printenv NAME | wc -c`).',
        )

    def keychain(self, call: Call) -> Denial | None:
        i = skip_options(call.args, 0, {"-p"})
        sub = call.args[i] if i < len(call.args) else ""
        if sub not in KEYCHAIN_READS:
            return None
        return Denial(
            "keychain-read",
            f"`security {sub}` reads the macOS keychain, which can print a stored password",
            "let the tool that needs the credential read it itself, or ask the person for "
            "what you need.",
        )

    def is_secret(self, file: str) -> bool:
        return self.matches(file, self.config.secret_globs) and not self.matches(
            file, self.config.secret_allow_globs
        )

    def matches(self, file: str, globs: list[str]) -> bool:
        name = PurePath(file).name.lower()
        paths = [file] + [p for p in (self.path(file, cwd) for cwd in self.cwds) if p]
        paths = [path.lower() for path in paths]
        for pattern in (item.lower() for item in globs):
            if "/" not in pattern and fnmatchcase(name, pattern):
                return True
            anchored = pattern if pattern.startswith("/") else "*/" + pattern
            if "/" in pattern and any(
                fnmatchcase(p, anchored) or fnmatchcase(p, pattern) for p in paths
            ):
                return True
        return False

    def safe_pipe(self, items: list[Command | str], index: int) -> bool:
        command = items[index]
        following = items[index + 1] if index + 1 < len(items) else None
        if not isinstance(command, Command) or command.after not in PIPES:
            return False
        return isinstance(following, Command) and self.safe_filter(following)

    def safe_redirect(self, call: Call | None, command: Command) -> bool:
        return call is not None and self.safe_filter(command)

    def safe_filter(self, command: Command) -> bool:
        call = unwrap(command.words)
        # Under xargs or `wc --files0-from` the input becomes file names, which errors print.
        if call and call.via_xargs:
            return False
        if call and call.program == "wc":
            return not any(reads_names(arg) for arg in call.args)
        if call and call.program == "cut" and names_only(call.args):
            return True
        if call and call.program in GREPS and reader_files(call.program, call.args) is None:
            return True
        text = " ".join(command.words)
        return any(pattern.search(text) for pattern in self.config.safe_filters)

    def database(
        self, call: Call, command: Command, items: list[Command | str], index: int
    ) -> Denial | None:
        for host in db_hosts(call):
            if any(pattern.search(host) for pattern in self.config.prod_db_hosts):
                return Denial(
                    "prod-db",
                    f"`{call.program}` connects to {host}, which matches prod_db_hosts",
                    "work against a local or staging database; production queries are for "
                    "the person to run.",
                )
        texts = call.args + command.stdin
        upstream = items[index - 1] if index > 0 else None
        if isinstance(upstream, Command) and upstream.after in PIPES:
            texts += upstream.words + upstream.stdin
        if any(DESTRUCTIVE_SQL.search(text) for text in texts):
            return Denial(
                "destructive-sql",
                f"`{call.program}` gets DROP DATABASE/TABLE/SCHEMA or TRUNCATE",
                "leave destructive SQL to the person, or use a migration or a reset command "
                "the project documents.",
            )
        return None


def deny(denial: Denial, warnings: list[str]) -> dict[str, Any]:
    reason = f"agent-harness guard, rule {denial.rule}: {denial.what}. To proceed: {denial.proceed}"
    if warnings:
        reason += " Guard config warnings: " + "; ".join(warnings) + "."
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def check(event: dict[str, Any], environ: Mapping[str, str]) -> tuple[Denial | None, list[str]]:
    """The denial for the tool call, if any, and the guard config warnings."""
    tool = event.get("tool_name")
    if tool != "Bash" and tool not in FILE_TOOLS:
        return None, []
    command = (event.get("tool_input") or {}).get(FILE_TOOLS.get(tool, "command"))
    if not isinstance(command, str) or not command.strip():
        return None, []
    if "\0" in command:
        return Denial(
            "unparsable",
            "the command or path holds a NUL character, which no shell or file system takes",
            "remove the NUL character.",
        ), []
    cwd = event.get("cwd") if isinstance(event.get("cwd"), str) else None
    project_dir = environ.get("CLAUDE_PROJECT_DIR") or cwd
    config = load_config(environ, Path(project_dir) if project_dir else None)
    agent = event.get("agent_type")
    subagent = bool(agent)
    bridge_only = config.scope != "all" and not subagent
    exempt = isinstance(agent, str) and agent in config.tampering_allow_agents
    writer = isinstance(agent, str) and agent in config.git_write_allow_agents
    checker = Checker(
        config, environ, cwd or project_dir, subagent, project_dir, exempt, writer, bridge_only
    )
    if tool != "Bash":
        return checker.tamper_denial(f"`{tool}`", [command]), config.warnings
    denial = checker.script(command)
    if bridge_only:
        return denial, config.warnings
    for pattern, reason in config.extra_deny:
        if denial is None and pattern.search(command):
            denial = Denial(
                "extra_deny", reason.rstrip("."), "change the command or ask the person."
            )
    return denial, config.warnings


def decision(event: dict[str, Any], environ: Mapping[str, str]) -> dict[str, Any] | None:
    denial, warnings = check(event, environ)
    return deny(denial, warnings) if denial else None


def log_guard_event(
    environ: Mapping[str, str],
    event: Mapping[str, Any],
    hook: str,
    rule: str,
    decision: str,
    now: datetime | None = None,
) -> None:
    """Append one line to `$HARNESS_HOME/guard-events.jsonl`; best effort, never the command."""
    line = {
        "time": (now or datetime.now(UTC)).isoformat(timespec="seconds"),
        "hook": hook,
        "session": event.get("session_id"),
        "agent_id": event.get("agent_id"),
        "agent_type": event.get("agent_type"),
        "tool": event.get("tool_name"),
        "rule": rule,
        "decision": decision,
    }
    try:
        path = harness_home(environ) / "guard-events.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as log:
            log.write(json.dumps(line, ensure_ascii=False) + "\n")
    except Exception as error:
        print(f"{hook} could not log the guard event: {error!r}", file=sys.stderr)


def internal_error(event: Any, error: Exception, stdout: TextIO, environ: Mapping[str, str]) -> int:
    """Deny a subagent's call the guard could not check; allow the main session's."""
    if not isinstance(event, dict) or not event.get("agent_type"):
        print(
            f"guard_shell.py allowed the command after an internal error: {error!r}",
            file=sys.stderr,
        )
        return 0
    print(f"guard_shell.py denied the call after an internal error: {error!r}", file=sys.stderr)
    denial = Denial(
        "guard_error",
        f"the guard could not check this call ({type(error).__name__}: {error})",
        "report the error to the lead and let it decide; the guard denies a subagent's "
        "call it could not check.",
    )
    json.dump(deny(denial, []), stdout)
    log_guard_event(environ, event, "guard_shell", "guard_error", "deny")
    return 0


def main(
    stdin: TextIO = sys.stdin,
    stdout: TextIO = sys.stdout,
    environ: Mapping[str, str] = os.environ,
) -> int:
    event: Any = None
    try:
        event = json.load(stdin)
        denial, warnings = check(event, environ)
    except Exception as error:
        return internal_error(event, error, stdout, environ)
    if denial is None:
        return 0
    json.dump(deny(denial, warnings), stdout)
    log_guard_event(environ, event, "guard_shell", denial.rule, "deny")
    return 0


if __name__ == "__main__":
    sys.exit(main())
