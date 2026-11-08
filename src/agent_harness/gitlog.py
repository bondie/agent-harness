"""What git knows about a ticket: the commits that name its key and the lines they changed."""

from __future__ import annotations

import fnmatch
import re
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .artifacts import Json, first_key

DEFAULT_TEST_PATTERNS = ("tests/", "spec/", "*Test.php", "*_test.py", "*.spec.ts")
RECORD = "\x1e"


@dataclass(frozen=True)
class FileChange:
    path: str
    added: int
    removed: int


@dataclass(frozen=True)
class Diff:
    files: tuple[str, ...]
    added: int
    removed: int
    test_added: int

    def as_json(self) -> Json:
        return {
            "files": list(self.files),
            "added": self.added,
            "removed": self.removed,
            "test_added": self.test_added,
        }


def git(repo: Path, *args: str) -> str:
    try:
        result = subprocess.run(  # noqa: S603
            ["git", "-c", "core.quotePath=false", "-C", str(repo), *args],  # noqa: S607
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except FileNotFoundError as error:
        raise ValueError("git is not installed") from error
    if result.returncode != 0:
        raise ValueError(f"git {args[0]} failed in {repo}: {result.stderr.strip()}")
    return result.stdout


def mentions(message: str, term: str) -> bool:
    """`term` as a whole word, so SHOP-41 does not match SHOP-412."""
    return re.search(rf"(?<![A-Za-z0-9]){re.escape(term)}(?![A-Za-z0-9])", message) is not None


def changes(repo: Path, term: str) -> list[FileChange]:
    """File changes of the commits on any ref whose message names `term`.

    A commit that is on two branches under different hashes (a cherry-pick, a rebased branch
    still pushed) has the same message and the same changes; it counts once.
    """
    output = git(
        repo,
        "log",
        "--all",
        "--fixed-strings",
        f"--grep={term}",
        "--numstat",
        f"--format={RECORD}%H%x00%B%x00",
    )
    seen: set[tuple[str, tuple[str, ...]]] = set()
    found: list[FileChange] = []
    for record in output.split(RECORD)[1:]:
        _, message, numstat = record.split("\x00", 2)
        lines = tuple(line for line in numstat.splitlines() if line.count("\t") >= 2)
        signature = (message.strip(), lines)
        if not mentions(message, term) or signature in seen:
            continue
        seen.add(signature)
        found += [_change(line) for line in lines]
    return found


def _change(line: str) -> FileChange:
    added, removed, path = line.split("\t", 2)
    return FileChange(
        path=new_path(path),
        added=int(added) if added.isdigit() else 0,
        removed=int(removed) if removed.isdigit() else 0,
    )


def new_path(path: str) -> str:
    """The path after a rename as `--numstat` prints it: `a/{old => new}/b` or `old => new`."""
    if " => " not in path:
        return path
    if "{" not in path or "}" not in path:
        return path.split(" => ", 1)[1]
    prefix, rest = path.split("{", 1)
    inner, suffix = rest.split("}", 1)
    return (prefix + inner.split(" => ", 1)[1] + suffix).replace("//", "/")


def is_test(path: str, patterns: Sequence[str]) -> bool:
    """`dir/` matches that directory anywhere in the path; a glob matches the file name, or
    the whole path when it contains a slash."""
    for pattern in patterns:
        if pattern.endswith("/"):
            if f"/{pattern}" in f"/{path}":
                return True
            continue
        target = path if "/" in pattern else PurePosixPath(path).name
        if fnmatch.fnmatchcase(target, pattern):
            return True
    return False


def diff(found: Sequence[FileChange], test_patterns: Sequence[str] = DEFAULT_TEST_PATTERNS) -> Diff:
    return Diff(
        files=tuple(sorted({change.path for change in found})),
        added=sum(change.added for change in found),
        removed=sum(change.removed for change in found),
        test_added=sum(change.added for change in found if is_test(change.path, test_patterns)),
    )


def resolve(repo: Path, commit: str) -> str:
    try:
        return git(repo, "rev-parse", "--verify", "--quiet", f"{commit}^{{commit}}").strip()
    except ValueError as error:
        raise ValueError(f"No commit {commit!r} in {repo}") from error


@dataclass(frozen=True)
class Attribution:
    ticket: str
    found_in: str


def ticket_of(repo: Path, commit: str) -> Attribution | None:
    """The ticket key of a commit: from its message, else from the first merge that brought it
    in (a merge request's merge commit carries the branch name and the title)."""
    key = first_key(git(repo, "log", "-1", "--format=%B", commit))
    if key:
        return Attribution(key, "the commit message")
    merges = git(
        repo,
        "log",
        "--all",
        f"^{commit}",
        "--ancestry-path",
        "--merges",
        "--topo-order",
        "--reverse",
        f"--format={RECORD}%B",
    ).split(RECORD)[1:]
    key = first_key(merges[0]) if merges else None
    if key:
        return Attribution(key, "the merge commit")
    return None
