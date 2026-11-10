"""Agent memory: `harness-eval memory lint|prune|compact` and the backtest's memory variants.

A lesson is one bullet that ends with its source and date, and optionally an expiry:
`- <lesson text> (source: <KEY|MR|commit|retro>, YYYY-MM-DD[, expires YYYY-MM-DD])`. Lines
indented by two spaces continue the bullet above; headings, prose, front matter, fenced code
and the index links in `MEMORY.md` are not lessons. The parsing mirrors
`hooks/guard_memory_writes.py`, which runs without this package; a test keeps them in step.

A backtest may show a ticket only the lessons learned before it was created and not yet
expired: the memory holds calibration from tickets that are in the test set, and a lesson
without a date cannot be placed in time, so it is left out.
"""

from __future__ import annotations

import hashlib
import json
import math
import posixpath
import re
import unicodedata
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from .artifacts import harness_home as harness_home

INDEX = "MEMORY.md"
BULLET = re.compile(r"[-*+]\s")
INDEX_LINK = re.compile(r"[-*+]\s+\[[^\]]+\]\((?![a-zA-Z][\w+.-]*:)[^)\s]+\.md\)")
TRAILER = re.compile(
    r"\((?:source:\s*(?P<source>[^,()]*?)\s*,\s*)?(?P<date>\d{4}-\d{2}-\d{2})"
    r"(?:\s*,\s*expires\s+(?P<expires>\d{4}-\d{2}-\d{2}))?\)[ \t]*$",
    re.M,
)
FENCE = re.compile(r"(`{3,}|~{3,})")
ABSOLUTE_PATH = re.compile(r"(?<![\w.~/:>}*-])~?/[^\s`'\"()\[\]{}<>,;:|*$]+")
PLACEHOLDER = set("<>{}*$")
EXCERPT = 80


@dataclass(frozen=True)
class Settings:
    max_file_kb: float = 8
    max_index_kb: float = 4
    require_source: bool = True
    warn_ratio: float = 0.8


@dataclass(frozen=True)
class Lesson:
    first: int
    last: int
    text: str
    source: str | None
    day: date | None
    expires: date | None

    @property
    def plain(self) -> str:
        """The lesson on one line, without its bullet marker."""
        return " ".join(line.strip() for line in self.text.splitlines())[1:].strip()

    def usable_on(self, day: date) -> bool:
        """Learned before `day` and not expired by then."""
        if self.day is None or self.day >= day:
            return False
        return self.expires is None or day < self.expires


@dataclass(frozen=True)
class Memory:
    lessons: tuple[Lesson, ...]

    @property
    def dated(self) -> int:
        return sum(1 for lesson in self.lessons if lesson.day is not None)

    @property
    def undated(self) -> int:
        return len(self.lessons) - self.dated

    def usable_on(self, created: str) -> list[Lesson]:
        """The lessons a ticket created at `created` (ISO date or timestamp) may see, by the
        local day, the day the lessons are dated in."""
        day = datetime.fromisoformat(created).astimezone().date()
        return [lesson for lesson in self.lessons if lesson.usable_on(day)]


@dataclass
class FileReport:
    path: Path
    size: int
    cap_kb: float
    lessons: int
    issues: list[tuple[Lesson, list[str]]] = field(default_factory=list)
    warn_ratio: float = Settings.warn_ratio

    @property
    def over_cap(self) -> bool:
        return self.size > self.cap_kb * 1024

    @property
    def near_cap(self) -> bool:
        """At `warn_ratio` of the cap or past it, and not over it."""
        return not self.over_cap and self.size >= self.warn_ratio * self.cap_kb * 1024


def _size_kb(value: Any, default: float) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return default
    if not math.isfinite(value) or value <= 0:
        return default
    return float(value)


def _ratio(value: Any, default: float) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return default
    if not math.isfinite(value) or not 0 < value <= 1:
        return default
    return float(value)


def load_settings(home: Path | None = None) -> Settings:
    """`memory.json` in the harness home; unusable values fall back to the defaults."""
    try:
        raw = json.loads(((home or harness_home()) / "memory.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    defaults = Settings()
    require_source = raw.get("require_source")
    return Settings(
        _size_kb(raw.get("max_file_kb"), defaults.max_file_kb),
        _size_kb(raw.get("max_index_kb"), defaults.max_index_kb),
        require_source if isinstance(require_source, bool) else defaults.require_source,
        _ratio(raw.get("warn_ratio"), defaults.warn_ratio),
    )


def _date(value: str | None) -> date | None:
    try:
        return date.fromisoformat(value or "")
    except ValueError:
        return None


def _opens(line: str) -> str:
    """The fence a line opens: a run of three or more backticks or tildes, or nothing."""
    match = FENCE.match(line)
    if match is None:
        return ""
    return match[1]


def _closes(line: str, fence: str) -> bool:
    """A run of the fence's character at least as long as it, with only spaces after."""
    return re.fullmatch(f"{re.escape(fence[0])}{{{len(fence)},}}[ \t]*", _bare(line)) is not None


def _front_matter_end(lines: list[str]) -> int:
    if not lines or lines[0].strip() != "---":
        return 0
    for number, line in enumerate(lines[1:], 1):
        if line.strip() == "---":
            return number + 1
    return 0


def _lesson(first: int, body: list[str]) -> Lesson:
    text = "\n".join(body)
    trailers = list(TRAILER.finditer(text))
    if not trailers:
        return Lesson(first, first + len(body) - 1, text, None, None, None)
    trailer = trailers[-1]
    source = (trailer["source"] or "").strip() or None
    day = _date(trailer["date"])
    return Lesson(first, first + len(body) - 1, text, source, day, _date(trailer["expires"]))


def lessons(text: str, index: bool = False) -> list[Lesson]:
    found: list[tuple[int, list[str]]] = []
    current: list[str] | None = None
    fence = ""
    lines = text.splitlines()
    start = _front_matter_end(lines)
    for number, line in enumerate(lines[start:], start + 1):
        if fence:
            if _closes(line, fence):
                fence = ""
            continue
        if _opens(line):
            fence, current = _opens(line), None
            continue
        if current is not None and line.startswith(("  ", "\t")) and line.strip():
            current.append(line.rstrip())
            continue
        current = None
        if not BULLET.match(line) or (index and INDEX_LINK.match(line)):
            continue
        current = [line.rstrip()]
        found.append((number, current))
    return [_lesson(number, body) for number, body in found]


def dead_paths(text: str) -> list[str]:
    """Absolute paths in the text that do not exist, when their top directory does.

    A path whose top directory is missing on this machine (`/api/v2/…`, a container's
    `/app/…`) is a URL or lives elsewhere, so it is not reported.
    """
    dead = []
    for match in ABSOLUTE_PATH.finditer(text):
        if text[match.end() : match.end() + 1] in PLACEHOLDER:
            continue
        token = match.group().rstrip(".")
        path = Path(token).expanduser()
        if len(path.parts) < 2 or not _exists(Path(*path.parts[:2])):
            continue
        if not _exists(path):
            dead.append(token)
    return dead


def _exists(path: Path) -> bool:
    try:
        return path.exists()
    except OSError:
        return True


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="surrogateescape")


def load_memory(directory: Path) -> Memory:
    """Every lesson in the Markdown files of one agent's memory directory."""
    if not directory.is_dir():
        raise ValueError(f"No memory directory at {directory}")
    found = [
        lesson
        for path in sorted(directory.rglob("*.md"))
        if path.is_file()
        for lesson in lessons(_read(path), path.name == INDEX)
    ]
    return Memory(tuple(found))


def memory_section(usable: Iterable[Lesson]) -> str:
    """The lessons as they would be loaded into the agent's prompt, or nothing."""
    lines = [f"- {lesson.plain}" for lesson in usable]
    if not lines:
        return ""
    return "\n\n## Your memory\n\nLessons recorded before this ticket was created:\n\n" + "\n".join(
        lines
    )


def lint_file(path: Path, settings: Settings, today: date) -> FileReport:
    text = _read(path)
    index = path.name == INDEX
    found = lessons(text, index)
    report = FileReport(
        path,
        len(text.encode("utf-8", errors="surrogateescape")),
        settings.max_index_kb if index else settings.max_file_kb,
        len(found),
        warn_ratio=settings.warn_ratio,
    )
    for lesson in found:
        issues = []
        if lesson.day is None:
            issues.append("undated")
        if settings.require_source and lesson.source is None:
            issues.append("no source")
        if lesson.expires is not None and lesson.expires <= today:
            issues.append(f"expired {lesson.expires}")
        issues += [f"dead path {dead}" for dead in dead_paths(lesson.text)]
        if issues:
            report.issues.append((lesson, issues))
    return report


def lint(root: Path, settings: Settings, today: date) -> dict[str, list[FileReport]]:
    if not root.is_dir():
        raise ValueError(f"No memory directory at {root}")
    return {
        agent.name: [
            lint_file(path, settings, today)
            for path in sorted(agent.rglob("*.md"))
            if path.is_file()
        ]
        for agent in sorted(root.iterdir())
        if agent.is_dir()
    }


def _count(reports: list[FileReport], prefix: str) -> int:
    return sum(
        1
        for report in reports
        for _, issues in report.issues
        for issue in issues
        if issue.startswith(prefix)
    )


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _summary(reports: list[FileReport], require_source: bool) -> str:
    parts = [
        _plural(len(reports), "file"),
        _plural(sum(report.lessons for report in reports), "lesson"),
        f"{_count(reports, 'undated')} undated",
        *([f"{_count(reports, 'no source')} without a source"] if require_source else []),
        f"{_count(reports, 'expired')} expired",
        _plural(_count(reports, "dead path"), "dead path"),
        _plural(sum(report.over_cap for report in reports), "file") + " over the cap",
        _plural(sum(report.near_cap for report in reports), "file") + " near the cap",
    ]
    return ", ".join(parts)


def _cap_note(report: FileReport) -> str:
    if report.over_cap:
        return f", over the {report.cap_kb:g} KB cap"
    if not report.near_cap:
        return ""
    share = report.size / (report.cap_kb * 1024) * 100
    return f", near the {report.cap_kb:g} KB cap ({share:.0f} %)"


def _excerpt(lesson: Lesson) -> str:
    first = lesson.text.splitlines()[0]
    return first if len(first) <= EXCERPT else first[: EXCERPT - 1] + "…"


def render_lint(results: dict[str, list[FileReport]], root: Path, settings: Settings) -> str:
    lines = []
    for agent, reports in results.items():
        lines.append(f"{agent}: {_summary(reports, settings.require_source)}")
        for report in reports:
            if not report.issues and not report.over_cap and not report.near_cap:
                continue
            name = report.path.relative_to(root / agent)
            lines.append(f"  {name}: {report.size / 1024:.1f} KB{_cap_note(report)}")
            lines += [
                f"    line {lesson.first}: {', '.join(issues)}: {_excerpt(lesson)}"
                for lesson, issues in report.issues
            ]
    everything = [report for reports in results.values() for report in reports]
    total = _summary(everything, settings.require_source)
    lines.append(f"Total, {_plural(len(results), 'agent')}: {total}")
    return "\n".join(lines) + "\n"


def has_findings(results: dict[str, list[FileReport]]) -> bool:
    return any(
        report.issues or report.over_cap for reports in results.values() for report in reports
    )


def expired(root: Path, today: date) -> list[tuple[Path, Lesson]]:
    if not root.is_dir():
        raise ValueError(f"No memory directory at {root}")
    return [
        (path, lesson)
        for path in sorted(root.glob("*/**/*.md"))
        if path.is_file()
        for lesson in lessons(_read(path), path.name == INDEX)
        if lesson.expires is not None and lesson.expires <= today
    ]


def remove(path: Path, gone: list[Lesson]) -> None:
    drop = {number for lesson in gone for number in range(lesson.first, lesson.last + 1)}
    lines = _read(path).splitlines(keepends=True)
    kept = [line for number, line in enumerate(lines, 1) if number not in drop]
    path.write_text("".join(kept), encoding="utf-8", errors="surrogateescape")


def prune(root: Path, today: date, apply: bool) -> str:
    found = expired(root, today)
    if not found:
        return "No expired lessons.\n"
    by_file: dict[Path, list[Lesson]] = {}
    for path, lesson in found:
        by_file.setdefault(path, []).append(lesson)
    lines = [
        f"{path.relative_to(root)} line {lesson.first}: expired {lesson.expires}: "
        f"{_excerpt(lesson)}"
        for path, lesson in found
    ]
    counted = f"{_plural(len(found), 'expired lesson')} in {_plural(len(by_file), 'file')}"
    if not apply:
        return "\n".join([*lines, f"{counted}. Dry run: add --apply to remove them."]) + "\n"
    for path, gone in by_file.items():
        remove(path, gone)
    return "\n".join([*lines, f"Removed {counted}."]) + "\n"


KEEP = "<!-- keep -->"
HEADING = re.compile(r"(#{1,6})[ \t]+(.*?)[ \t]*$")
LINE = re.compile(r"[^\n]*\n|[^\n]+")
LINK_TARGET = re.compile(r"\]\(([^)\s]+\.md)\)")
MEMORY_TYPE = re.compile(
    r"^metadata:[ \t]*\r?\n(?:[ \t]+\S.*\r?\n)*?[ \t]+type:[ \t]*([^\s\"']+)", re.M
)
Numbered = list[tuple[int, str]]


class CompactionError(ValueError):
    """The memory changed since the proposal, or a check after the move failed."""


@dataclass(frozen=True)
class Move:
    source: Path
    target: Path
    title: str
    first: int
    last: int
    lessons: int
    size: int
    new: bool


@dataclass(frozen=True)
class Section:
    start: int
    end: int
    title: str
    size: int


@dataclass
class Compaction:
    """A proposal of verbatim moves: the files as read, as they will be, and why.

    `sizes` holds per file at its warning mark the size before and after, its cap and whether
    it stays at the mark; `pointers` and `front_matter` are the only lines the moves add.
    """

    root: Path
    moves: list[Move] = field(default_factory=list)
    sizes: dict[Path, tuple[int, int, float, bool]] = field(default_factory=dict)
    notes: dict[Path, list[str]] = field(default_factory=dict)
    before: dict[Path, str | None] = field(default_factory=dict)
    after: dict[Path, str] = field(default_factory=dict)
    pointers: list[str] = field(default_factory=list)
    front_matter: list[str] = field(default_factory=list)
    caps: dict[Path, float] = field(default_factory=dict)


def _load(path: Path) -> str | None:
    """The exact text, line endings included, or None for a missing file."""
    try:
        return path.read_bytes().decode("utf-8", errors="surrogateescape")
    except FileNotFoundError:
        return None


def _temporary(path: Path) -> Path:
    return path.with_name(f".{path.name}.compact")


def _store(path: Path, text: str, unchanged: str | None = None, check: bool = False) -> str:
    """Writes through a temporary file and returns what it wrote; with `check`, only over the
    text the plan read."""
    temporary = _temporary(path)
    temporary.write_bytes(text.encode("utf-8", errors="surrogateescape"))
    if check and _digest(_load(path)) != _digest(unchanged):
        temporary.unlink()
        raise CompactionError(
            f"{path} changed during the apply, an agent may be writing to it: every file is "
            "restored; run compact again when no agent runs"
        )
    temporary.replace(path)
    return text


def _digest(text: str | None) -> str | None:
    if text is None:
        return None
    return hashlib.sha256(text.encode("utf-8", errors="surrogateescape")).hexdigest()


def _bytes(text: str) -> int:
    return len(text.encode("utf-8", errors="surrogateescape"))


def _bare(line: str) -> str:
    return line.rstrip("\r\n")


def _newline(text: str) -> str:
    if "\r\n" in text:
        return "\r\n"
    return "\n"


def _headings(lines: list[str]) -> list[tuple[int, int, str, bool]]:
    """(line index, level, title, kept) of each heading outside front matter and fenced code.

    A heading is kept when it or the line right below it carries `<!-- keep -->`, or when a
    heading above it of a higher level is kept.
    """
    found: list[tuple[int, int, str, bool]] = []
    open_levels: list[tuple[int, bool]] = []
    fence = ""
    for number in range(_front_matter_end([_bare(line) for line in lines]), len(lines)):
        bare = _bare(lines[number])
        if fence:
            if _closes(bare, fence):
                fence = ""
            continue
        if _opens(bare):
            fence = _opens(bare)
            continue
        match = HEADING.match(bare)
        if match is None:
            continue
        level = len(match[1])
        while open_levels and open_levels[-1][0] >= level:
            open_levels.pop()
        below = "".join(lines[number + 1 : number + 2]).strip()
        kept = KEEP in bare or below == KEEP or any(kept for _, kept in open_levels)
        open_levels.append((level, kept))
        found.append((number, level, match[2].replace(KEEP, "").strip(), kept))
    return found


def movable_sections(lines: list[str], index: bool) -> list[Section]:
    """Headed sections that may move: none kept inside, no index link in MEMORY.md, and not the
    one section that spans every heading of the file (its title)."""
    heads = _headings(lines)
    found = []
    for position, (start, level, title, _) in enumerate(heads):
        ends = [head[0] for head in heads[position + 1 :] if head[1] <= level]
        end = [*ends, len(lines)][0]
        inside = [head for head in heads[position:] if head[0] < end]
        if any(kept for *_, kept in inside) or (position == 0 and end == len(lines)):
            continue
        block = lines[start:end]
        if index and any(INDEX_LINK.match(_bare(line)) for line in block):
            continue
        found.append(Section(start, end, title, _bytes("".join(block))))
    return found


def _slug(title: str) -> str:
    plain = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", plain.lower()).strip("-")[:40].strip("-") or "section"


def _links(index_text: str) -> set[str]:
    return {
        posixpath.normpath(match[1])
        for line in _lines(index_text)
        if INDEX_LINK.match(_bare(line))
        for match in LINK_TARGET.finditer(_bare(line))
    }


def _lines(text: str) -> list[str]:
    return LINE.findall(text)


def _text(numbered: Numbered) -> str:
    return "".join(line for _, line in numbered)


def _terminated(numbered: Numbered) -> Numbered:
    """The lines with a line ending on the last one, so that more lines can follow."""
    if not numbered or numbered[-1][1].endswith("\n"):
        return numbered
    number, line = numbered[-1]
    return [*numbered[:-1], (number, line + _newline(_text(numbered)))]


def _with_pointer(numbered: Numbered, pointer: str) -> Numbered:
    """The index with the pointer after its last index link, or at its end."""
    links = [at for at, (_, line) in enumerate(numbered) if INDEX_LINK.match(_bare(line))]
    added = (0, pointer + _newline(_text(numbered)))
    if not links:
        return [*_terminated(numbered), added]
    return [*_terminated(numbered[: links[-1] + 1]), added, *numbered[links[-1] + 1 :]]


def _memory_type(text: str) -> str:
    """`metadata.type` from the front matter, `reference` without one."""
    lines = _lines(text)
    head = "".join(lines[: _front_matter_end([_bare(line) for line in lines])])
    match = MEMORY_TYPE.search(head)
    if match is None:
        return "reference"
    return match[1]


class _Planner:
    """Plans the moves of one agent's memory in memory; nothing is written."""

    def __init__(self, plan: Compaction, directory: Path, settings: Settings) -> None:
        self.plan, self.directory, self.settings = plan, directory, settings
        self.index = directory / INDEX
        self.files: dict[Path, Numbered] = {}

    def lines(self, path: Path) -> Numbered:
        if path not in self.files:
            original = _load(path)
            self.plan.before[path] = original
            self.files[path] = list(enumerate(_lines(original or ""), 1))
        return self.files[path]

    def size(self, path: Path) -> int:
        return _bytes(_text(self.lines(path)))

    def cap(self, path: Path) -> float:
        if path == self.index:
            return self.settings.max_index_kb
        return self.settings.max_file_kb

    def mark(self, path: Path) -> float:
        return self.settings.warn_ratio * self.cap(path) * 1024

    def note(self, source: Path, text: str) -> None:
        notes = self.plan.notes.setdefault(source, [])
        if text not in notes:
            notes.append(text)

    def relative(self, path: Path) -> str:
        return path.relative_to(self.directory).as_posix()

    def takes(self, path: Path, title: str, part: Numbered) -> bool:
        """An existing topic file takes a section of its own title: its first heading is the
        title, its pointer in MEMORY.md names it, and the section fits under the mark."""
        heads = _headings([line for _, line in self.lines(path)])
        if not heads or heads[0][2] != title:
            return False
        pointers = [
            line
            for _, line in self.lines(self.index)
            if INDEX_LINK.match(_bare(line)) and self.relative(path) in _links(line)
        ]
        if not any(title in line for line in pointers):
            return False
        return _bytes(_text([*_terminated(self.lines(path)), *part])) < self.mark(path)

    def target(self, source: Path, title: str, part: Numbered) -> tuple[Path, bool]:
        """A new `<slug>.md` (`<stem>-<slug>.md` from a topic file), or one of the same title."""
        base = _slug(title)
        if source != self.index:
            base = f"{source.stem}-{base}"
        for name in [f"{base}.md", *(f"{base}-{number}.md" for number in range(2, 100))]:
            path = source.parent / name
            if path == source:
                continue
            if path not in self.files and not path.exists():
                return path, True
            if self.takes(path, title, part):
                return path, False
        raise CompactionError(f"No free topic file name for {title!r} next to {source}")

    def compact(self, source: Path) -> None:
        """Moves sections until the file is under its mark; when the sections that stay keep
        it at the mark, nothing moves, unless the file is over its cap: then only until it is
        under the cap."""
        before = self.size(source)
        saved = (dict(self.files), dict(self.plan.before), len(self.plan.moves))
        saved_added = (len(self.plan.pointers), len(self.plan.front_matter))
        self.shrink(source, self.mark(source))
        if self.size(source) >= self.mark(source) and len(self.plan.moves) > saved[2]:
            self.files, self.plan.before = saved[0], saved[1]
            del self.plan.moves[saved[2] :]
            del self.plan.pointers[saved_added[0] :]
            del self.plan.front_matter[saved_added[1] :]
            self.note(
                source,
                f"left: {self.relative(source)} cannot get under the mark, only kept or refused "
                "sections remain",
            )
            if before > self.cap(source) * 1024:
                self.shrink(source, math.floor(self.cap(source) * 1024) + 1)
        after = self.size(source)
        self.plan.sizes[source] = (before, after, self.cap(source), after >= self.mark(source))

    def shrink(self, source: Path, limit: float) -> None:
        """Moves the largest section that may move, then the next, while the file is at the
        limit or past it."""
        refused: set[int] = set()
        while self.size(source) >= limit:
            numbered = self.lines(source)
            sections = [
                section
                for section in movable_sections(
                    [line for _, line in numbered], source == self.index
                )
                if numbered[section.start][0] not in refused
            ]
            if not sections:
                self.note(
                    source,
                    "no section left to move: the rest is kept sections, index links, the "
                    "title section and the lines before the first heading",
                )
                return
            section = max(sections, key=lambda item: (item.size, -item.start))
            if not self.move(source, section):
                refused.add(numbered[section.start][0])

    def front(self, source: Path, target: Path, title: str) -> Numbered:
        """The front matter of a new topic file, in the format of the agents' memory."""
        text = _text(self.lines(source))
        description = json.dumps(f"{title}, moved from {self.relative(source)}", ensure_ascii=False)
        lines = [
            "---",
            f"name: {target.stem}",
            f"description: {description}",
            "metadata:",
            f"  type: {_memory_type(text)}",
            "---",
        ]
        return [(0, line + _newline(text)) for line in lines]

    def move(self, source: Path, section: Section) -> bool:
        numbered = self.lines(source)
        part = numbered[section.start : section.end]
        target, new = self.target(source, section.title, part)
        rest = numbered[: section.start] + numbered[section.end :]
        index = self.lines(self.index)
        if source == self.index:
            index = rest
        title = section.title.replace("[", "(").replace("]", ")")
        pointer = f"- [{title}]({self.relative(target)}) — moved from {self.relative(source)}"
        if new:
            index = _with_pointer(index, pointer)
        grown = _bytes(_text(index)) > self.size(self.index)
        if new and grown and _bytes(_text(index)) > self.cap(self.index) * 1024:
            self.note(
                source, f"left: the pointer for {section.title!r} would push {INDEX} over its cap"
            )
            return False
        current = self.front(source, target, section.title)
        if not new:
            current = self.lines(target)
        filled = [*_terminated(current), *part]
        if _bytes(_text(filled)) > self.cap(target) * 1024:
            self.note(
                source,
                f"left: {section.title!r} is larger than the {self.cap(target):g} KB cap of a "
                "topic file",
            )
            return False
        self.plan.before.setdefault(target, None)
        self.files[target] = filled
        if source != self.index:
            self.files[source] = rest
        self.files[self.index] = index
        if new:
            self.plan.pointers.append(pointer)
            self.plan.front_matter += [_bare(line) for _, line in current]
        block = _text(part)
        self.plan.moves.append(
            Move(
                source,
                target,
                section.title,
                part[0][0],
                part[-1][0],
                len(lessons(block)),
                _bytes(block),
                new,
            )
        )
        return True


def _agents(root: Path, agent: str | None, file: Path | None) -> list[tuple[Path, Path | None]]:
    if not root.is_dir():
        raise ValueError(f"No memory directory at {root}")
    if file is not None:
        resolved = file.expanduser().resolve()
        try:
            parts = resolved.relative_to(root.resolve()).parts
        except ValueError:
            raise ValueError(f"{file} is not in an agent directory under {root}") from None
        if len(parts) < 2 or not resolved.is_file():
            raise ValueError(f"{file} is not a memory file under {root}")
        return [(root / parts[0], root.joinpath(*parts))]
    if agent is not None:
        if not (root / agent).is_dir():
            raise ValueError(f"No agent directory {agent} under {root}")
        return [(root / agent, None)]
    return [
        (path, None)
        for path in sorted(root.iterdir())
        if path.is_dir() and not path.name.startswith(".")
    ]


def plan_compaction(
    root: Path, settings: Settings, agent: str | None = None, file: Path | None = None
) -> Compaction:
    """Verbatim moves of whole sections out of each file at its warning mark or past it.

    The largest movable section goes first, until the file is under the mark; the lines
    before the first heading, sections marked `<!-- keep -->` with their subsections and, in
    MEMORY.md, the sections holding index links stay. A section that cannot move is left and
    the next one tried. Deterministic: the same files give the same proposal.
    """
    plan = Compaction(root)
    for directory, only in _agents(root, agent, file):
        planner = _Planner(plan, directory, settings)
        topics = sorted(
            path for path in directory.rglob("*.md") if path.is_file() and path.name != INDEX
        )
        sources = [path for path in [planner.index, *topics] if path.is_file()]
        for source in sources:
            if only is not None and source.resolve() != only.resolve():
                continue
            if planner.size(source) >= planner.mark(source):
                planner.compact(source)
        for path, numbered in planner.files.items():
            plan.caps[path] = planner.cap(path)
            if _text(numbered) != (plan.before[path] or ""):
                plan.after[path] = _text(numbered)
    return plan


def _kb(size: int, cap_kb: float) -> str:
    return f"{size / 1024:.1f} KB ({size / (cap_kb * 1024) * 100:.0f} % of {cap_kb:g} KB)"


def render_plan(plan: Compaction) -> str:
    lines = []
    for source, (before, after, cap_kb, _) in plan.sizes.items():
        lines.append(
            f"{source.relative_to(plan.root)}: {_kb(before, cap_kb)} -> {_kb(after, cap_kb)}"
        )
        for move in plan.moves:
            if move.source != source:
                continue
            target = move.target.relative_to(_agent_dir(plan, move.source))
            linked = ""
            if move.new:
                linked = " (new, linked from MEMORY.md)"
            lines.append(
                f"  move {move.title!r}, lines {move.first}-{move.last}, "
                f"{_plural(move.lessons, 'lesson')}, {move.size / 1024:.1f} KB, to {target}{linked}"
            )
        lines += [f"  {note}" for note in plan.notes.get(source, [])]
    if not plan.sizes:
        return "No memory file is at its warning mark.\n"
    left = sum(1 for *_, stays in plan.sizes.values() if stays)
    lines.append(
        f"{_plural(len(plan.moves), 'move')} from {_plural(len(plan.sizes), 'file')}; "
        f"{_plural(left, 'file')} left at the warning mark."
    )
    return "\n".join(lines) + "\n"


def _agent_dir(plan: Compaction, path: Path) -> Path:
    return plan.root / path.relative_to(plan.root).parts[0]


def _lesson_texts(texts: dict[Path, str]) -> Counter[str]:
    return Counter(
        lesson.text for path, text in texts.items() for lesson in lessons(text, path.name == INDEX)
    )


def _line_texts(texts: dict[Path, str]) -> Counter[str]:
    return Counter(_bare(line) for text in texts.values() for line in _lines(text))


def check_moves(plan: Compaction, after: dict[Path, str | None]) -> None:
    """The files after the moves against the files before them; CompactionError on a failure.

    The lessons are the same, every line is there exactly once and byte for byte (only the
    pointers and the front matter of new files are new), every moved section stands whole in
    its target, every new target is linked from MEMORY.md and no file grew over its cap.
    """
    old = {path: plan.before.get(path) or "" for path in plan.after}
    new = {path: after.get(path) or "" for path in plan.after}
    if _lesson_texts(old) != _lesson_texts(new):
        raise CompactionError("the lessons are not the same after the move")
    added = Counter(plan.pointers) + Counter(plan.front_matter)
    if _line_texts(old) + added != _line_texts(new):
        raise CompactionError("a line is missing, doubled or changed after the move")
    for move in plan.moves:
        moved = _text(_moved_lines(plan, move))
        if _bare(moved) not in new.get(move.target, ""):
            raise CompactionError(f"{move.title!r} does not stand whole in {move.target}")
        directory = _agent_dir(plan, move.target)
        index = new.get(directory / INDEX)
        if index is None:
            index = plan.before.get(directory / INDEX) or ""
        if move.target.relative_to(directory).as_posix() not in _links(index):
            raise CompactionError(f"{move.target} is not linked from {INDEX}")
    for path, text in new.items():
        if _bytes(text) > plan.caps[path] * 1024 and _bytes(text) > _bytes(old[path]):
            raise CompactionError(f"{path} would grow over its {plan.caps[path]:g} KB cap")


def _moved_lines(plan: Compaction, move: Move) -> Numbered:
    """The moved lines, as they stood in the source when it was read."""
    numbered = list(enumerate(_lines(plan.before.get(move.source) or ""), 1))
    return [(number, line) for number, line in numbered if move.first <= number <= move.last]


def _write_order(plan: Compaction) -> list[Path]:
    """New files and targets first, the sources next, MEMORY.md last: a stop midway leaves a
    section in two places, never in none."""
    sources = {move.source for move in plan.moves}

    def rank(path: Path) -> int:
        if path.name == INDEX:
            return 3
        if path in sources:
            return 2
        if plan.before.get(path) is None:
            return 0
        return 1

    return sorted(plan.after, key=lambda path: (rank(path), str(path)))


def apply_plan(plan: Compaction) -> None:
    """Writes the moves after checking them, reads them back and checks again.

    A file that changed since the proposal stops it before any write, and one that changes
    during it (an agent writing) before its own rename. Any failure or interruption puts back
    each file the apply wrote while it still holds what the apply wrote there; a file an agent
    changed after that is kept and named. A failed write or check comes back as
    CompactionError, an interruption as itself with the kept files in a note.
    """
    for path, text in plan.before.items():
        if _digest(_load(path)) != _digest(text):
            raise CompactionError(f"{path} changed since the proposal: run compact again")
    check_moves(plan, dict(plan.after))
    written: dict[Path, str] = {}
    try:
        for path in _write_order(plan):
            written[path] = _store(path, plan.after[path], plan.before.get(path), check=True)
        check_moves(plan, {path: _load(path) for path in plan.after})
    except BaseException as error:
        kept = _restore(plan, written)
        if not isinstance(error, OSError | CompactionError):
            if kept:
                error.add_note(_kept(kept))
            raise
        reason = str(error)
        if isinstance(error, OSError):
            reason = f"a write failed: {error}"
        raise CompactionError(f"{reason}. {_kept(kept)}") from error


def _kept(kept: list[Path]) -> str:
    if not kept:
        return "Every file the apply wrote is restored."
    names = ", ".join(str(path) for path in kept)
    return (
        f"The other files are restored; kept as they are, since an agent changed them during "
        f"the apply: {names}."
    )


def _restore(plan: Compaction, written: dict[Path, str]) -> list[Path]:
    """Puts back each file the apply wrote while it holds what the apply wrote there, and
    returns the ones an agent changed after that, which it keeps; a file the apply did not
    write stays as it is."""
    kept = []
    for path in plan.after:
        _temporary(path).unlink(missing_ok=True)
        ours = written.get(path, plan.after[path])
        if _load(path) != ours:
            if path in written:
                kept.append(path)
            continue
        original = plan.before.get(path)
        if original is None:
            path.unlink(missing_ok=True)
            continue
        _store(path, original)
    return kept
