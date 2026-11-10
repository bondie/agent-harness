"""Agent memory lessons: `harness-eval memory lint|prune` and the backtest's memory variants.

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

import json
import math
import re
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
            fence = "" if line.startswith(fence) else fence
            continue
        if line.startswith(("```", "~~~")):
            fence, current = line[:3], None
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
