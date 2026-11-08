"""What git knows about a ticket: the commits that name its key and the lines they changed."""

from __future__ import annotations

import fnmatch
import re
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .artifacts import Json, ticket_keys

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


def _log(repo: Path, rev: str | None, term: str | None) -> str:
    grep = ["--fixed-strings", f"--grep={term}"] if term else []
    return git(
        repo,
        "log",
        rev or "--all",
        *grep,
        "--numstat",
        f"--format={RECORD}%H%x00%at%x00%B%x00",
    )


def _by_term(output: str, terms: Sequence[str]) -> dict[str, list[FileChange]]:
    """A commit on two branches under different hashes (a cherry-pick, a rebased branch still
    pushed) keeps its author time, message and changes; it counts once."""
    seen: dict[str, set[tuple[str, str, tuple[str, ...]]]] = {term: set() for term in terms}
    found: dict[str, list[FileChange]] = {term: [] for term in terms}
    for record in output.split(RECORD)[1:]:
        _, authored, message, numstat = record.split("\x00", 3)
        lines = tuple(line for line in numstat.splitlines() if line.count("\t") >= 2)
        signature = (authored, message.strip(), lines)
        for term in terms:
            if term not in message or not mentions(message, term) or signature in seen[term]:
                continue
            seen[term].add(signature)
            found[term] += [_change(line) for line in lines]
    return found


def changes(repo: Path, term: str, rev: str | None = None) -> list[FileChange]:
    """File changes of the commits whose message names `term`, on `rev` or on any ref."""
    return _by_term(_log(repo, rev, term), [term])[term]


def changes_by_term(
    repo: Path, terms: Sequence[str], rev: str | None = None
) -> dict[str, list[FileChange]]:
    """`changes` for many terms from one pass over the log."""
    unique = list(dict.fromkeys(terms))
    return _by_term(_log(repo, rev, None), unique) if unique else {}


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
    candidates: tuple[str, ...]
    found_in: str


def ticket_of(repo: Path, commit: str) -> Attribution | None:
    """The ticket keys of a commit: from its message, else from the first merge that brought
    its branch in (a merge request's merge commit carries the branch name and the title).

    A merge brought the commit in only when the commit is on its second parent's side and
    not on its first: a commit made on the target branch belongs to no later merge.
    """
    commit = resolve(repo, commit)
    keys = ticket_keys(git(repo, "log", "-1", "--format=%B", commit))
    if keys:
        return Attribution(tuple(keys), "the commit message")
    path = ["log", "--all", f"^{commit}", "--ancestry-path"]
    descendants = {commit, *git(repo, *path, "--format=%H").split()}
    merges = git(
        repo, *path, "--merges", "--topo-order", "--reverse", f"--format={RECORD}%P%x00%B"
    ).split(RECORD)[1:]
    for merge in merges:
        parents, message = merge.split("\x00", 1)
        first, second = parents.split()[:2]
        if second not in descendants or first in descendants:
            continue
        keys = ticket_keys(message)
        return Attribution(tuple(keys), "the merge commit") if keys else None
    return None
