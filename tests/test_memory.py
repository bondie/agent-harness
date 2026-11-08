import calendar
import importlib.util
import itertools
import json
import time
from datetime import date, timedelta
from pathlib import Path
from types import ModuleType

import pytest

from agent_harness import cli, memory
from agent_harness.memory import Lesson, Memory, Settings, load_memory, memory_section

HOOK = Path(__file__).resolve().parents[1] / "hooks" / "guard_memory_writes.py"
TODAY = date(2026, 3, 1)

SAMPLE = """---
name: notes
- a front matter list item
---
# Notes

- [Commands](commands.md) — how to run the tests
- An old note without a date
- Run the specs with make spec (source: SHOP-412, 2026-01-15)
- A lesson that wraps
  onto a second line (source: !786, 2026-01-15, expires 2026-02-01)
- A date alone (2026-01-15)
* A star bullet (source: retro, 2026-01-15)
Prose ends the lesson above.
  An indented line after prose is not a lesson.
```yaml
- not: a lesson
```
- Ends in prose (source: SHOP-1, 2026-01-15) and more
"""


def load_hook() -> ModuleType:
    spec = importlib.util.spec_from_file_location("guard_memory_writes", HOOK)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_lessons_carry_their_source_date_and_expiry():
    found = memory.lessons(SAMPLE, index=True)

    assert [(lesson.first, lesson.last) for lesson in found] == [
        (8, 8),
        (9, 9),
        (10, 11),
        (12, 12),
        (13, 13),
        (19, 19),
    ]
    wrapped = found[2]
    assert wrapped.source == "!786"
    assert wrapped.day == date(2026, 1, 15)
    assert wrapped.expires == date(2026, 2, 1)
    assert found[0].day is None
    assert found[0].source is None
    assert (found[3].source, found[3].day) == (None, date(2026, 1, 15))
    assert found[4].source == "retro"
    assert found[5].day is None


def test_an_index_link_is_a_lesson_outside_memory_md():
    assert memory.lessons(SAMPLE, index=False)[0].text.startswith("- [Commands]")


def test_an_unclosed_front_matter_is_a_rule():
    assert [lesson.first for lesson in memory.lessons("---\n- A lesson\n")] == [2]


@pytest.mark.parametrize("index", [True, False])
def test_the_hook_and_the_cli_read_lessons_alike(index):
    hook = load_hook()
    for name in ("BULLET", "INDEX_LINK", "TRAILER"):
        assert getattr(hook, name).pattern == getattr(memory, name).pattern
    found = memory.lessons(SAMPLE, index)

    assert [(lesson.first, lesson.text) for lesson in found] == hook.lessons(SAMPLE, index)
    for lesson in found:
        complete = lesson.day is not None and lesson.source is not None
        assert (hook.problem(lesson.text, True) is None) == complete


def test_dead_paths_are_missing_paths_under_an_existing_top_directory(tmp_path):
    (tmp_path / "kept.txt").write_text("x")
    text = (
        f"`{tmp_path}/kept.txt`, {tmp_path}/gone/file.txt. {tmp_path}/<project>/x "
        f"{tmp_path}/{{file}} /api/v2/shop/cart https://example.com{tmp_path}/gone "
        f"src/{tmp_path} ~/"
    )

    assert memory.dead_paths(text) == [f"{tmp_path}/gone/file.txt"]


def test_a_path_that_cannot_be_checked_is_not_dead(tmp_path, monkeypatch):
    def refuse(path: Path, **options: bool) -> bool:
        raise PermissionError(path)

    monkeypatch.setattr(Path, "exists", refuse)

    assert memory.dead_paths(f"{tmp_path}/locked") == []


def fixture_memory(root: Path, gone: Path) -> Path:
    implementer = root / "agent-harness-implementer"
    implementer.mkdir(parents=True)
    (implementer / "MEMORY.md").write_text(SAMPLE)
    (implementer / "topic.md").write_text(
        "# Topic\n\n"
        f"- Rebuild it from scratch; it was in {gone}/scratch (source: SHOP-2, 2026-01-15)\n"
        "- Fine (source: SHOP-3, 2026-01-15, expires 2999-01-01)\n"
    )
    estimator = root / "agent-harness-estimator"
    estimator.mkdir()
    (estimator / "MEMORY.md").write_text("- Calibrated (source: SHOP-4, 2026-01-15)\n")
    (root / "empty-agent").mkdir()
    (root / "stray.md").write_text("- Not in an agent directory\n")
    return root


def test_lint_lists_each_agent_and_file(tmp_path):
    root = fixture_memory(tmp_path / "agent-memory", tmp_path / "gone")
    settings = Settings(max_index_kb=0.25)

    results = memory.lint(root, settings, TODAY)
    lines = memory.render_lint(results, root, settings).splitlines()

    assert list(results) == ["agent-harness-estimator", "agent-harness-implementer", "empty-agent"]
    assert lines[:7] == [
        "agent-harness-estimator: 1 file, 1 lesson, 0 undated, 0 without a source, 0 expired, "
        "0 dead paths, 0 files over the cap",
        "agent-harness-implementer: 2 files, 8 lessons, 2 undated, 3 without a source, "
        "1 expired, 1 dead path, 1 file over the cap",
        "  MEMORY.md: 0.5 KB, over the 0.25 KB cap",
        "    line 8: undated, no source: - An old note without a date",
        "    line 10: expired 2026-02-01: - A lesson that wraps",
        "    line 12: no source: - A date alone (2026-01-15)",
        "    line 19: undated, no source: - Ends in prose (source: SHOP-1, 2026-01-15) and more",
    ]
    assert lines[7].startswith("  topic.md: ")
    assert "cap" not in lines[7]
    dead = f"    line 3: dead path {tmp_path}/gone/scratch: - Rebuild it from scratch; it was in "
    assert lines[8].startswith(dead)
    assert lines[8].endswith("…")
    assert len(lines[8]) == len(dead) - len("- Rebuild it from scratch; it was in ") + 80
    assert lines[9:] == [
        "empty-agent: 0 files, 0 lessons, 0 undated, 0 without a source, 0 expired, 0 dead paths, "
        "0 files over the cap",
        "Total, 3 agents: 3 files, 9 lessons, 2 undated, 3 without a source, 1 expired, "
        "1 dead path, 1 file over the cap",
    ]
    assert memory.has_findings(results)


def test_lint_skips_the_source_when_it_is_optional(tmp_path):
    root = fixture_memory(tmp_path / "agent-memory", tmp_path / "gone")
    settings = Settings(require_source=False)

    output = memory.render_lint(memory.lint(root, settings, TODAY), root, settings)

    assert "without a source" not in output
    assert "no source" not in output
    assert "    line 8: undated: - An old note without a date" in output


def test_a_clean_memory_has_no_findings(tmp_path):
    (tmp_path / "agent").mkdir()
    (tmp_path / "agent" / "MEMORY.md").write_text("- Fine (source: SHOP-1, 2026-01-15)\n")

    assert not memory.has_findings(memory.lint(tmp_path, Settings(), TODAY))


def test_cli_lint(tmp_path, monkeypatch, capsys):
    root = fixture_memory(tmp_path / "agent-memory", tmp_path / "gone")
    home = tmp_path / "harness"
    home.mkdir()
    (home / "memory.json").write_text(json.dumps({"max_file_kb": 0.05}))
    monkeypatch.setenv("HARNESS_HOME", str(home))

    assert cli.main(["memory", "lint", "--dir", str(root)]) == 1
    output = capsys.readouterr().out
    assert " KB, over the 0.05 KB cap\n" in output
    assert "  MEMORY.md: 0.5 KB\n    line 8: undated, no source" in output

    clean = tmp_path / "clean" / "agent"
    clean.mkdir(parents=True)
    assert cli.main(["memory", "lint", "--dir", str(clean.parent)]) == 0
    assert cli.main(["memory", "lint", "--dir", str(tmp_path / "missing")]) == 1
    assert "No memory directory" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('{"max_file_kb": 16, "max_index_kb": 2.5, "require_source": false}', (16, 2.5, False)),
        ('{"max_file_kb": NaN, "max_index_kb": Infinity, "require_source": "no"}', (8, 4, True)),
        ('{"max_file_kb": -1, "max_index_kb": 0, "require_source": null}', (8, 4, True)),
        ('{"max_file_kb": "16", "max_index_kb": true}', (8, 4, True)),
        ("[16]", (8, 4, True)),
        ("not json", (8, 4, True)),
    ],
)
def test_settings(tmp_path, text, expected):
    (tmp_path / "memory.json").write_text(text)
    assert memory.load_settings(tmp_path) == Settings(*expected)


def test_settings_default_without_a_file(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_HOME", str(tmp_path))
    assert memory.load_settings() == Settings()
    monkeypatch.delenv("HARNESS_HOME")
    assert memory.harness_home() == Path.home() / ".claude" / "agent-harness"


EXPIRING = (
    "# Notes\n"
    "- Kept (source: SHOP-1, 2019-01-01)\n"
    "- Gone (source: SHOP-2, 2019-01-01, expires 2020-01-01)\n"
    "  with its continuation\n"
    "- Kept until later (source: SHOP-3, 2019-01-01, expires 2999-01-01)\n"
)


def expiring_memory(root: Path) -> Path:
    path = root / "agent" / "sub" / "topic.md"
    path.parent.mkdir(parents=True)
    path.write_bytes(EXPIRING.encode() + b"Prose with a stray byte \xff\n")
    return path


def test_prune_is_a_dry_run_by_default(tmp_path, capsys):
    path = expiring_memory(tmp_path)

    assert cli.main(["memory", "prune", "--expired", "--dir", str(tmp_path)]) == 0

    assert capsys.readouterr().out.splitlines() == [
        "agent/sub/topic.md line 3: expired 2020-01-01: "
        "- Gone (source: SHOP-2, 2019-01-01, expires 2020-01-01)",
        "1 expired lesson in 1 file. Dry run: add --apply to remove them.",
    ]
    assert path.read_bytes().startswith(EXPIRING.encode())


def test_prune_apply_removes_expired_lessons_with_their_continuation(tmp_path, capsys):
    path = expiring_memory(tmp_path)

    assert cli.main(["memory", "prune", "--expired", "--apply", "--dir", str(tmp_path)]) == 0

    assert capsys.readouterr().out.splitlines()[-1] == "Removed 1 expired lesson in 1 file."
    assert path.read_bytes() == (
        b"# Notes\n"
        b"- Kept (source: SHOP-1, 2019-01-01)\n"
        b"- Kept until later (source: SHOP-3, 2019-01-01, expires 2999-01-01)\n"
        b"Prose with a stray byte \xff\n"
    )
    assert memory.prune(tmp_path, date.today(), apply=True) == "No expired lessons.\n"


def test_prune_counts_files_and_lessons(tmp_path):
    expiring_memory(tmp_path / "one")
    second = expiring_memory(tmp_path / "two")
    second.write_text(EXPIRING + "- Also gone (source: retro, 2019-01-01, expires 2019-06-01)\n")

    output = memory.prune(tmp_path, TODAY, apply=False)

    assert (
        output.splitlines()[-1]
        == "3 expired lessons in 2 files. Dry run: add --apply to remove them."
    )


def test_prune_needs_the_expired_flag_and_a_directory(tmp_path, capsys):
    with pytest.raises(SystemExit):
        cli.main(["memory", "prune", "--dir", str(tmp_path)])
    assert cli.main(["memory", "prune", "--expired", "--dir", str(tmp_path / "missing")]) == 1
    assert "No memory directory" in capsys.readouterr().err


BACKTEST_MEMORY = """---
name: estimator memory
description: calibration
---

# Estimator memory

- [Release timings](release.md) — per package
- Release takes about 1 min per package (source: retro, 2026-02-10)
- Bumps run in parallel, a few minutes each
  (source: SHOP-412, 2026-03-01, expires 2026-06-01)
* A trailing period breaks the format (source: MR, 2026-04-01).
- A lesson dated in the middle (source: retro, 2026-01-01) is not in the contract format
- Correction (2026-05-01): a date in the text is not a source tag

| Ticket | Hours |
|---|---|
| SHOP-1 | 2 |

```text
- not a lesson (source: retro, 2026-01-01)
```
  an indented line after a code block belongs to nothing
"""


def test_the_backtest_reads_dated_lessons_on_one_line():
    found = memory.lessons(BACKTEST_MEMORY, index=True)

    assert [(lesson.day, lesson.expires) for lesson in found] == [
        (date(2026, 2, 10), None),
        (date(2026, 3, 1), date(2026, 6, 1)),
        (None, None),
        (None, None),
        (None, None),
    ]
    assert found[1].plain == (
        "Bumps run in parallel, a few minutes each (source: SHOP-412, 2026-03-01, expires "
        "2026-06-01)"
    )
    assert found[2].plain.startswith("A trailing period")


def made(text: str, day: str | None = None, expires: str | None = None) -> Lesson:
    def parsed(value: str | None) -> date | None:
        return date.fromisoformat(value) if value else None

    return Lesson(1, 1, f"- {text}", "retro" if day else None, parsed(day), parsed(expires))


def brute_force_usable(lesson: Lesson, day: date) -> bool:
    if lesson.day is None:
        return False
    expires = lesson.expires or date.max
    return lesson.day < day < expires


LESSONS = [
    made("undated"),
    made("early", "2026-01-10"),
    made("expiring", "2026-01-10", "2026-01-20"),
    made("late", "2026-02-01"),
]


@pytest.mark.parametrize(("lesson", "offset"), list(itertools.product(LESSONS, range(40))))
def test_a_ticket_sees_only_lessons_learned_before_it_and_not_expired(lesson, offset):
    day = date(2026, 1, 1) + timedelta(days=offset)
    assert lesson.usable_on(day) == brute_force_usable(lesson, day)


@pytest.fixture
def local_zone(monkeypatch):
    def use(name):
        monkeypatch.setenv("TZ", name)
        time.tzset()

    yield use
    monkeypatch.undo()
    time.tzset()


@pytest.mark.parametrize("zone", ["UTC", "Europe/Prague", "America/New_York", "Asia/Tokyo"])
def test_a_ticket_created_near_midnight_sees_the_lessons_of_its_local_day(local_zone, zone):
    local_zone(zone)
    agent_memory = Memory(tuple(LESSONS))
    for day in ("2026-01-10", "2026-01-11", "2026-01-20", "2026-02-01"):
        for minutes in range(-15 * 60, 15 * 60 + 1, 20):
            moment = date.fromisoformat(day)
            epoch = calendar.timegm(moment.timetuple()) + minutes * 60
            utc = time.gmtime(epoch)
            created = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", utc)
            local = time.localtime(epoch)
            local_day = date(local.tm_year, local.tm_mon, local.tm_mday)
            expected = [lesson for lesson in LESSONS if brute_force_usable(lesson, local_day)]
            assert agent_memory.usable_on(created) == expected, created


def test_memory_filters_by_the_tickets_creation_day(local_zone):
    local_zone("UTC")
    agent_memory = Memory(tuple(LESSONS))

    assert agent_memory.dated == 3
    assert agent_memory.undated == 1
    assert agent_memory.usable_on("2026-01-10T23:59:00+00:00") == []
    assert [lesson.plain for lesson in agent_memory.usable_on("2026-01-15T08:00:00+00:00")] == [
        "early",
        "expiring",
    ]
    assert [lesson.plain for lesson in agent_memory.usable_on("2026-03-01")] == [
        "early",
        "late",
    ]


def test_load_memory_reads_every_markdown_file(tmp_path):
    (tmp_path / "MEMORY.md").write_text(BACKTEST_MEMORY)
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "topic.md").write_text("- topic lesson (source: retro, 2026-01-05)\n")
    (tmp_path / "notes.txt").write_text("- ignored (source: retro, 2026-01-05)\n")
    (tmp_path / "index.md").write_text("- [Link](other.md) — a lesson outside MEMORY.md\n")

    agent_memory = load_memory(tmp_path)

    assert (agent_memory.dated, agent_memory.undated) == (3, 4)


def test_load_memory_needs_a_directory(tmp_path):
    with pytest.raises(ValueError, match="No memory directory"):
        load_memory(tmp_path / "missing")


def test_memory_section():
    assert memory_section([]) == ""
    wrapped = memory.lessons("- early\n  wrapped (source: retro, 2026-01-10)")
    assert memory_section(wrapped).endswith(
        "before this ticket was created:\n\n- early wrapped (source: retro, 2026-01-10)"
    )
