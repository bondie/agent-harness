import json
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from agent_harness import cli, memory
from agent_harness.memory import CompactionError, Settings

SMALL = Settings(max_index_kb=1, max_file_kb=2)
LINK = "- [Commands](commands.md) — how to run the tests\n"
TRAPS_POINTER = "- [Traps](traps.md) — moved from MEMORY.md"
FRONT = (
    '---\nname: {name}\ndescription: "{title}, moved from {source}"\n'
    "metadata:\n  type: {kind}\n---\n"
)


def lessons_of(topic: str, count: int, newline: str = "\n") -> str:
    return "".join(
        f"- {topic} lesson number {n} about a thing (source: SHOP-{n}, 2026-01-15){newline}"
        for n in range(count)
    )


def index_text(keep: str = "## Every run <!-- keep -->\n", newline: str = "\n") -> str:
    text = (
        "# Memory\n\nPreamble.\n\n"
        + keep
        + lessons_of("Keep", 3)
        + "### Detail\n"
        + lessons_of("Sub", 2)
        + "\n## Topics\n"
        + LINK
        + "\n## Traps\n"
        + lessons_of("Trap", 5)
        + "\n## Commands\n"
        + lessons_of("Cmd", 2)
    )
    return text.replace("\n", newline)


COMMANDS = (
    '---\nname: commands\ndescription: "Commands"\nmetadata:\n  type: feedback\n---\n'
    "# Commands\n## Build\n" + lessons_of("Build", 20) + "## Deploy\n" + lessons_of("Deploy", 8)
)


def agent(root: Path, name: str = "agent", index: str | None = None) -> Path:
    directory = root / name
    directory.mkdir(parents=True)
    if index is None:
        index = index_text()
    (directory / "MEMORY.md").write_bytes(index.encode())
    return directory


def two_sources(root: Path) -> Path:
    directory = agent(root)
    (directory / "commands.md").write_text(COMMANDS)
    return directory


def snapshot(root: Path) -> dict[Path, bytes]:
    return {path: path.read_bytes() for path in sorted(root.rglob("*")) if path.is_file()}


def all_lessons(root: Path) -> Counter[str]:
    return Counter(
        lesson.text
        for path in root.rglob("*.md")
        for lesson in memory.lessons(
            path.read_bytes().decode("utf-8", "surrogateescape"), path.name == "MEMORY.md"
        )
    )


def test_the_proposal_is_deterministic_and_writes_nothing(tmp_path):
    two_sources(tmp_path)
    before = snapshot(tmp_path)

    first = memory.render_plan(memory.plan_compaction(tmp_path, SMALL))
    second = memory.render_plan(memory.plan_compaction(tmp_path, SMALL))

    assert first == second
    assert first.splitlines() == [
        "agent/MEMORY.md: 0.9 KB (91 % of 1 KB) -> 0.6 KB (62 % of 1 KB)",
        "  move 'Traps', lines 16-22, 5 lessons, 0.3 KB, to traps.md (new, linked from MEMORY.md)",
        "agent/commands.md: 2.0 KB (98 % of 2 KB) -> 0.6 KB (31 % of 2 KB)",
        "  move 'Build', lines 8-28, 20 lessons, 1.3 KB, to commands-build.md (new, linked from "
        "MEMORY.md)",
        "2 moves from 2 files; 0 files left at the warning mark.",
    ]
    assert snapshot(tmp_path) == before


def test_the_largest_movable_section_goes_and_its_pointer_follows_the_last_link(tmp_path):
    directory = agent(tmp_path)

    memory.apply_plan(memory.plan_compaction(tmp_path, SMALL))

    index = (directory / "MEMORY.md").read_text()
    assert "## Traps" not in index
    assert f"{LINK}{TRAPS_POINTER}\n\n## Commands\n" in index
    assert (directory / "traps.md").read_text() == (
        FRONT.format(name="traps", title="Traps", source="MEMORY.md", kind="reference")
        + "## Traps\n"
        + lessons_of("Trap", 5)
        + "\n"
    )
    assert len(index.encode()) < SMALL.warn_ratio * 1024


@pytest.mark.parametrize(
    "keep", ["## Every run <!-- keep -->\n", "## Every run\n<!-- keep -->\n", "## Every run\n"]
)
def test_kept_sections_their_subsections_and_index_links_never_move(tmp_path, keep):
    big = (
        "# Memory\n"
        + keep
        + lessons_of("Keep", 4)
        + "### Detail\n"
        + lessons_of("Sub", 9)
        + "## Topics\n"
        + LINK * 6
    )
    agent(tmp_path, index=big)

    plan = memory.plan_compaction(tmp_path, SMALL)

    titles = [move.title for move in plan.moves]
    assert "Topics" not in titles
    if keep == "## Every run\n":
        assert titles == ["Every run"]
        return
    assert titles == []
    assert "no section left to move" in memory.render_plan(plan)


def test_an_oversized_topic_file_is_split_next_to_it_with_its_type_and_linked(tmp_path):
    directory = agent(tmp_path, index="# Memory\n" + LINK)
    (directory / "commands.md").write_text(COMMANDS)

    plan = memory.plan_compaction(tmp_path, SMALL)
    memory.apply_plan(plan)

    assert [(move.title, move.target.name) for move in plan.moves] == [
        ("Build", "commands-build.md")
    ]
    assert (directory / "commands-build.md").read_text() == (
        FRONT.format(name="commands-build", title="Build", source="commands.md", kind="feedback")
        + "## Build\n"
        + lessons_of("Build", 20)
    )
    assert (
        (directory / "commands.md")
        .read_text()
        .endswith("# Commands\n## Deploy\n" + lessons_of("Deploy", 8))
    )
    assert (directory / "MEMORY.md").read_text() == (
        f"# Memory\n{LINK}- [Build](commands-build.md) — moved from commands.md\n"
    )


def test_a_topic_file_of_its_own_title_takes_the_section(tmp_path):
    index = index_text().replace(LINK, LINK + "- [Traps](traps.md) — Traps of the old kind\n")
    directory = agent(tmp_path, index=index)
    (directory / "traps.md").write_text("# Traps\n- Old (source: SHOP-1, 2026-01-15)")

    plan = memory.plan_compaction(tmp_path, SMALL)
    memory.apply_plan(plan)

    assert [(move.target.name, move.new) for move in plan.moves] == [("traps.md", False)]
    assert (directory / "traps.md").read_text() == (
        "# Traps\n- Old (source: SHOP-1, 2026-01-15)\n## Traps\n" + lessons_of("Trap", 5) + "\n"
    )
    assert (plan.pointers, plan.front_matter) == ([], [])


@pytest.mark.parametrize(
    ("pointer", "content"),
    [
        ("- [Traps](traps.md) — Traps of the old kind\n", "x" * 1600),
        ("", "x" * 1600),
        ("", "# Traps\n"),
        ("- [Pitfalls](traps.md) — pitfalls\n", "# Traps\n"),
        ("- [Traps](traps.md) — Traps of the old kind\n", "# Snags\n"),
        ("", "x" * 10),
    ],
)
def test_a_name_taken_by_another_file_gets_a_suffix(tmp_path, pointer, content):
    directory = agent(tmp_path, index=index_text().replace(LINK, LINK + pointer))
    (directory / "traps.md").write_text(content)

    plan = memory.plan_compaction(tmp_path, SMALL)

    assert [(move.target.name, move.new) for move in plan.moves] == [("traps-2.md", True)]


def test_two_titles_with_one_slug_get_their_own_files_and_pointers(tmp_path):
    prefix = "Commands for the local development stack"
    index = (
        "# Memory\n"
        + LINK
        + f"## {prefix} start\n"
        + lessons_of("Start", 6)
        + f"## {prefix} stop\n"
        + lessons_of("Stop", 5)
    )
    directory = agent(tmp_path, index=index)

    plan = memory.plan_compaction(tmp_path, Settings(max_index_kb=1, max_file_kb=2, warn_ratio=0.3))
    memory.apply_plan(plan)

    slug = "commands-for-the-local-development-stack"
    assert [(move.title, move.target.name) for move in plan.moves] == [
        (f"{prefix} start", f"{slug}.md"),
        (f"{prefix} stop", f"{slug}-2.md"),
    ]
    text = (directory / "MEMORY.md").read_text()
    assert f"- [{prefix} start]({slug}.md) — moved from MEMORY.md\n" in text
    assert f"- [{prefix} stop]({slug}-2.md) — moved from MEMORY.md\n" in text


def test_a_section_that_cannot_move_lets_the_next_one_go(tmp_path):
    index = (
        "# Memory\n"
        + LINK
        + "## Huge\n"
        + lessons_of("Huge", 20)
        + "## Medium\n"
        + lessons_of("Medium", 8)
    )
    agent(tmp_path, index=index)

    plan = memory.plan_compaction(tmp_path, Settings(max_index_kb=2, max_file_kb=1))

    assert [move.title for move in plan.moves] == ["Medium"]
    output = memory.render_plan(plan)
    assert "  left: 'Huge' is larger than the 1 KB cap of a topic file\n" in output
    assert "1 move from 1 file; 0 files left at the warning mark." in output


def test_a_pointer_over_the_index_cap_lets_the_next_section_go(tmp_path):
    directory = agent(tmp_path, index="# Memory\n" + LINK * 20)
    (directory / "commands.md").write_text(
        "# Commands\n## Build\n" + lessons_of("Build", 20) + "## Deploy\n" + lessons_of("Deploy", 8)
    )

    plan = memory.plan_compaction(tmp_path, SMALL)

    output = memory.render_plan(plan)
    assert "left: the pointer for 'Build' would push MEMORY.md over its cap" in output
    assert "left: the pointer for 'Deploy' would push MEMORY.md over its cap" in output
    assert plan.moves == []


def test_a_heading_inside_a_longer_fence_is_not_a_heading():
    text = (
        "# Title\n## A\n````md\n```\n# comment\n```\n````\n- a (source: SHOP-1, 2026-01-15)\n"
        "## B\n- b (source: SHOP-2, 2026-01-15)\n"
    )

    sections = memory.movable_sections(text.splitlines(keepends=True), index=False)

    assert [section.title for section in sections] == ["A", "B"]


def edge_case(name: str) -> bytes:
    if name == "multi-line lessons":
        return (
            index_text()
            .replace("(source: SHOP-1,", "with\n  a continuation (source: SHOP-1,")
            .encode()
        )
    if name == "fenced code":
        return (
            index_text()
            .replace("## Traps\n", "## Traps\n```text\n## not a heading\n```\n")
            .encode()
        )
    if name == "a stray byte":
        return (
            index_text()
            .replace("Trap lesson number 2", "Trap lesson \udcff 2")
            .encode("utf-8", "surrogateescape")
        )
    if name == "no trailing newline":
        text = index_text()
        start = text.index("## Traps")
        end = text.index("## Commands")
        return (text[:start] + text[end:] + "\n" + text[start:end]).rstrip("\n").encode()
    return index_text(newline="\r\n").encode()


@pytest.mark.parametrize(
    "case",
    ["multi-line lessons", "fenced code", "a stray byte", "no trailing newline", "CRLF"],
)
def test_apply_moves_the_lines_byte_for_byte(tmp_path, case):
    directory = agent(tmp_path, index="")
    (directory / "MEMORY.md").write_bytes(edge_case(case))
    before = snapshot(tmp_path)
    lessons_before = all_lessons(tmp_path)

    plan = memory.plan_compaction(tmp_path, SMALL)
    memory.apply_plan(plan)

    assert [move.title for move in plan.moves] == ["Traps"]
    original = before[directory / "MEMORY.md"]
    start = original.index(b"## Traps")
    block = original[start:]
    following = original.find(b"## Commands", start)
    if following > start:
        block = original[start:following]
    target = (directory / "traps.md").read_bytes()
    assert target.startswith(b"---")
    assert block.rstrip(b"\r\n") in target
    assert block.rstrip(b"\r\n") not in (directory / "MEMORY.md").read_bytes()
    assert all_lessons(tmp_path) == lessons_before
    if case == "CRLF":
        for path in (directory / "MEMORY.md", directory / "traps.md"):
            assert b"\n" not in path.read_bytes().replace(b"\r\n", b"")
    assert memory.plan_compaction(tmp_path, SMALL).moves == []


def failing_store(
    monkeypatch: pytest.MonkeyPatch, at: int, error: BaseException, when: str
) -> None:
    """Make the `at`-th write of the apply fail: before it, after its temporary file or after
    its rename."""
    store = memory._store
    calls = []

    def store_then_fail(path: Path, text: str, *args: Any, **options: Any) -> str:
        calls.append(path)
        failing = len(calls) - 1 == at
        if failing and when == "temporary":
            memory._temporary(path).write_bytes(b"partial")
        if failing and when != "after":
            raise error
        written = store(path, text, *args, **options)
        if failing:
            raise error
        return written

    monkeypatch.setattr(memory, "_store", store_then_fail)


@pytest.mark.parametrize("when", ["before", "temporary", "after"])
@pytest.mark.parametrize("at", [0, 1, 2, 3])
def test_an_interrupt_at_any_write_restores_every_file(tmp_path, monkeypatch, at, when):
    two_sources(tmp_path)
    before = snapshot(tmp_path)
    plan = memory.plan_compaction(tmp_path, SMALL)
    assert len(plan.after) == 4
    failing_store(monkeypatch, at, KeyboardInterrupt(), when)

    with pytest.raises(KeyboardInterrupt):
        memory.apply_plan(plan)

    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("at", [0, 1, 2, 3])
def test_a_hard_stop_at_any_write_leaves_duplicates_not_losses(tmp_path, monkeypatch, at):
    two_sources(tmp_path)
    lessons_before = all_lessons(tmp_path)
    plan = memory.plan_compaction(tmp_path, SMALL)
    failing_store(monkeypatch, at, KeyboardInterrupt(), "after")
    monkeypatch.setattr(memory, "_restore", lambda plan, written: None)

    with pytest.raises(KeyboardInterrupt):
        memory.apply_plan(plan)

    assert lessons_before - all_lessons(tmp_path) == Counter()


def test_a_failed_write_midway_restores_every_file(tmp_path, monkeypatch):
    two_sources(tmp_path)
    before = snapshot(tmp_path)
    plan = memory.plan_compaction(tmp_path, SMALL)
    failing_store(monkeypatch, 2, OSError(28, "No space left on device"), "temporary")

    with pytest.raises(
        CompactionError, match=r"a write failed: .*\. Every file the apply wrote is restored\."
    ):
        memory.apply_plan(plan)

    assert snapshot(tmp_path) == before


def test_an_agent_write_during_the_apply_stops_it_and_is_kept(tmp_path, monkeypatch):
    directory = two_sources(tmp_path)
    before = snapshot(tmp_path)
    plan = memory.plan_compaction(tmp_path, SMALL)
    store = memory._store
    agent_line = "- Written by an agent meanwhile (source: SHOP-9, 2026-01-15)\n"

    def agent_writes(path: Path, text: str, *args: Any, **options: Any) -> str:
        if path.name == "MEMORY.md" and options.get("check"):
            with path.open("a") as handle:
                handle.write(agent_line)
        return store(path, text, *args, **options)

    monkeypatch.setattr(memory, "_store", agent_writes)

    with pytest.raises(CompactionError, match="changed during the apply"):
        memory.apply_plan(plan)

    after = snapshot(tmp_path)
    index = directory / "MEMORY.md"
    assert after[index] == before[index] + agent_line.encode()
    assert {path: data for path, data in after.items() if path != index} == {
        path: data for path, data in before.items() if path != index
    }


def test_a_file_an_agent_changed_after_its_write_is_kept_and_named(tmp_path, monkeypatch):
    directory = two_sources(tmp_path)
    before = snapshot(tmp_path)
    plan = memory.plan_compaction(tmp_path, SMALL)
    first = memory._write_order(plan)[0]
    store = memory._store
    calls = []
    lesson = "- Added by an agent meanwhile (source: SHOP-9, 2026-01-15)\n"

    def agent_then_failure(path: Path, text: str, *args: Any, **options: Any) -> str:
        calls.append(path)
        if len(calls) == 3:
            raise OSError(28, "No space left on device")
        written = store(path, text, *args, **options)
        if path == first:
            with path.open("a") as handle:
                handle.write(lesson)
        return written

    monkeypatch.setattr(memory, "_store", agent_then_failure)

    with pytest.raises(CompactionError) as raised:
        memory.apply_plan(plan)

    assert first == directory / "commands-build.md"
    assert f"since an agent changed them during the apply: {first}." in str(raised.value)
    assert first.read_text() == plan.after[first] + lesson
    assert {path: data for path, data in snapshot(tmp_path).items() if path != first} == before


def test_an_interrupt_names_a_file_an_agent_changed(tmp_path, monkeypatch):
    two_sources(tmp_path)
    plan = memory.plan_compaction(tmp_path, SMALL)
    first = memory._write_order(plan)[0]
    store = memory._store

    def agent_then_interrupt(path: Path, text: str, *args: Any, **options: Any) -> str:
        written = store(path, text, *args, **options)
        if path != first:
            raise KeyboardInterrupt
        with path.open("a") as handle:
            handle.write("- Added meanwhile (source: SHOP-9, 2026-01-15)\n")
        return written

    monkeypatch.setattr(memory, "_store", agent_then_interrupt)

    with pytest.raises(KeyboardInterrupt) as raised:
        memory.apply_plan(plan)

    assert any(str(first) in note for note in raised.value.__notes__)


def kept_topic(small: int) -> str:
    return (
        "# Commands\n## Every run <!-- keep -->\n"
        + lessons_of("Kept", 105)
        + "".join(f"## Small {n}\n" + lessons_of(f"Small{n}", 1) for n in range(small))
    )


def test_a_file_the_kept_sections_hold_at_the_mark_moves_nothing(tmp_path):
    directory = agent(tmp_path, index="# Memory\n" + LINK)
    (directory / "commands.md").write_text(kept_topic(8))
    size = len((directory / "commands.md").read_bytes())
    assert 0.8 * 8192 <= size <= 8192

    plan = memory.plan_compaction(tmp_path, Settings())

    assert (plan.moves, plan.after, plan.pointers, plan.front_matter) == ([], {}, [], [])
    assert (
        "  left: commands.md cannot get under the mark, only kept or refused sections remain\n"
        in (memory.render_plan(plan))
    )


def test_a_file_over_its_cap_moves_only_until_it_is_under_the_cap(tmp_path):
    directory = agent(tmp_path, index="# Memory\n" + LINK)
    (directory / "commands.md").write_text(kept_topic(20))
    assert len((directory / "commands.md").read_bytes()) > 8192

    plan = memory.plan_compaction(tmp_path, Settings())
    memory.apply_plan(plan)

    size = len((directory / "commands.md").read_bytes())
    assert 0.8 * 8192 <= size <= 8192
    assert size + len(plan.moves[-1].target.read_bytes()) > 8192
    assert "cannot get under the mark" in memory.render_plan(plan)


@pytest.mark.parametrize(
    ("dropped", "problem"),
    [
        (lessons_of("Trap", 5).splitlines(keepends=True)[2], "the lessons are not the same"),
        ("## Traps\n", "a line is missing, doubled or changed"),
    ],
)
def test_a_dropped_line_is_caught_and_every_file_restored(tmp_path, monkeypatch, dropped, problem):
    agent(tmp_path)
    before = snapshot(tmp_path)
    plan = memory.plan_compaction(tmp_path, SMALL)
    store = memory._store

    def lossy(path: Path, text: str, *args: Any, **options: Any) -> str:
        if path.name == "traps.md":
            text = text.replace(dropped, "")
        return store(path, text, *args, **options)

    monkeypatch.setattr(memory, "_store", lossy)

    with pytest.raises(CompactionError, match=problem):
        memory.apply_plan(plan)
    assert snapshot(tmp_path) == before


def test_a_section_out_of_order_in_its_target_is_caught(tmp_path, monkeypatch):
    agent(tmp_path)
    before = snapshot(tmp_path)
    plan = memory.plan_compaction(tmp_path, SMALL)
    store = memory._store

    def reordered(path: Path, text: str, *args: Any, **options: Any) -> str:
        if path.name == "traps.md":
            lines = text.splitlines(keepends=True)
            lines[7], lines[8] = lines[8], lines[7]
            text = "".join(lines)
        return store(path, text, *args, **options)

    monkeypatch.setattr(memory, "_store", reordered)

    with pytest.raises(CompactionError, match="does not stand whole in"):
        memory.apply_plan(plan)
    assert snapshot(tmp_path) == before


def test_a_new_target_without_a_pointer_is_caught(tmp_path):
    index, target = tmp_path / "agent" / "MEMORY.md", tmp_path / "agent" / "traps.md"
    block = "## Traps\n- A trap (source: SHOP-1, 2026-01-15)\n"
    plan = memory.Compaction(
        tmp_path,
        moves=[memory.Move(index, target, "Traps", 1, 2, 1, len(block), True)],
        before={index: block, target: None},
        after={index: "", target: block},
        caps={index: 4, target: 8},
    )

    with pytest.raises(CompactionError, match=r"is not linked from MEMORY\.md"):
        memory.check_moves(plan, dict(plan.after))


def test_a_section_larger_than_a_topic_file_cap_stays(tmp_path):
    agent(tmp_path)

    plan = memory.plan_compaction(tmp_path, Settings(max_index_kb=1, max_file_kb=0.2))

    assert plan.moves == []
    output = memory.render_plan(plan)
    assert "left: 'Traps' is larger than the 0.2 KB cap of a topic file" in output
    assert "left: 'Commands' is larger than the 0.2 KB cap of a topic file" in output


def test_a_file_changed_after_the_proposal_is_refused_before_any_write(tmp_path):
    directory = agent(tmp_path)
    plan = memory.plan_compaction(tmp_path, SMALL)
    with (directory / "MEMORY.md").open("a") as handle:
        handle.write("- A new lesson (source: SHOP-9, 2026-01-15)\n")
    before = snapshot(tmp_path)

    with pytest.raises(CompactionError, match="changed since the proposal"):
        memory.apply_plan(plan)
    assert snapshot(tmp_path) == before


def test_a_check_catches_a_target_over_its_cap(tmp_path):
    agent(tmp_path)
    plan = memory.plan_compaction(tmp_path, SMALL)
    plan.caps[plan.moves[0].target] = 0.1

    with pytest.raises(CompactionError, match=r"would grow over its 0\.1 KB cap"):
        memory.apply_plan(plan)
    assert not plan.moves[0].target.exists()


def test_cli_compact(tmp_path, monkeypatch, capsys):
    root = tmp_path / "memory"
    agent(root)
    agent(root, "other", index="# Memory\n")
    home = tmp_path / "harness"
    home.mkdir()
    (home / "memory.json").write_text(json.dumps({"max_index_kb": 1, "max_file_kb": 2}))
    monkeypatch.setenv("HARNESS_HOME", str(home))
    before = snapshot(root)

    def run(*arguments: str) -> tuple[int, str, str]:
        code = cli.main(["memory", "compact", "--dir", str(root), *arguments])
        captured = capsys.readouterr()
        return code, captured.out, captured.err

    code, out, _ = run()
    assert (code, out.splitlines()[-1]) == (0, "Proposal only: add --apply to make the moves.")
    assert snapshot(root) == before
    assert run("--agent", "other")[1] == "No memory file is at its warning mark.\n"
    assert "move 'Traps'" in run("--file", str(root / "agent" / "MEMORY.md"))[1]
    assert "No memory file" in run("--file", str(root / "other" / "MEMORY.md"))[1]
    code, _, err = run("--file", str(tmp_path / "elsewhere.md"))
    assert (code, "is not in an agent directory" in err) == (1, True)
    code, _, err = run("--agent", "missing")
    assert (code, "No agent directory missing" in err) == (1, True)

    code, out, _ = run("--apply")
    assert (code, out.splitlines()[-1]) == (
        0,
        "Moved and checked: 1 sections, lessons and lines unchanged.",
    )
    assert (root / "agent" / "traps.md").is_file()
    assert run("--apply")[1] == "No memory file is at its warning mark.\n"
    assert cli.main(["memory", "compact", "--dir", str(tmp_path / "missing")]) == 1
