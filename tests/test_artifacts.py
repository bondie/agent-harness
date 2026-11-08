import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from tests.test_hooks import HOOKS, hook

ANALYSIS = {
    "ticket": "SHOP-412",
    "repositories": ["shop"],
    "plan": [{"repo": "shop", "file": "src/feed.py", "change": "Retry", "tests": True}],
}
REVIEW = {"ticket": "SHOP-412", "verdict": "pass", "findings": []}


def fenced(kind: str, body: Any) -> str:
    text = body if isinstance(body, str) else json.dumps(body)
    return f"```json harness-{kind}\n{text}\n```"


def user(text: str, stamp: str = "2026-03-04T10:00:00Z", **fields: Any) -> dict[str, Any]:
    return {
        "type": "user",
        "timestamp": stamp,
        "message": {"role": "user", "content": text},
        **fields,
    }


def said(text: str, message_id: str = "m1") -> dict[str, Any]:
    content = [{"type": "text", "text": text}]
    return {"type": "assistant", "message": {"id": message_id, "content": content}}


def handed_back(text: str) -> dict[str, Any]:
    content = [{"type": "tool_use", "name": "SubagentHandback", "input": {"message": text}}]
    return {"type": "assistant", "message": {"id": "h1", "content": content}}


def transcript(tmp_path: Path, *entries: dict[str, Any], name: str = "agent-a1") -> Path:
    path = tmp_path / "subagents" / f"{name}.jsonl"
    path.parent.mkdir(exist_ok=True)
    path.write_text("\n".join([*map(json.dumps, entries), "not json"]) + "\n")
    return path


def stop(
    tmp_path: Path,
    report: str = "Done.",
    agent: str | None = "agent-harness:reviewer-qa",
    prompt: str = "Review SHOP-412 please",
    agent_id: str = "a1",
    **fields: Any,
) -> dict[str, Any]:
    event: dict[str, Any] = {
        "hook_event_name": "SubagentStop",
        "agent_id": agent_id,
        "agent_transcript_path": str(
            transcript(tmp_path, user(prompt), said(report), name=f"agent-{agent_id}")
        ),
        "stop_hook_active": False,
        **fields,
    }
    if agent:
        event["agent_type"] = agent
    return event


def save(tmp_path: Path, event: dict[str, Any], **env: str) -> Path:
    result = hook(
        "save_artifacts.py",
        event,
        **{"HARNESS_HOME": str(tmp_path / "home"), "HARNESS_TICKET": "", **env},
    )
    assert result.returncode == 0
    assert result.stdout == ""
    return tmp_path / "home" / "tickets"


def index(directory: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in (directory / "index.jsonl").read_text().splitlines()]


def read(path: Path) -> Any:
    return json.loads(path.read_text())


def test_the_report_and_its_block_are_saved_under_the_ticket(tmp_path):
    report = f"Verdict: approve.\n\n{fenced('review', REVIEW)}"

    ticket = save(tmp_path, stop(tmp_path, report)) / "SHOP-412"

    assert (ticket / "reviewer-qa.01.md").read_text() == report + "\n"
    assert read(ticket / "review.json") == REVIEW
    [line] = index(ticket)
    assert line["agent"] == "reviewer-qa"
    assert line["agent_id"] == "a1"
    assert line["report"] == "reviewer-qa.01.md"
    assert line["blocks"] == ["review"]
    assert datetime.fromisoformat(line["ended"]).tzinfo is not None
    assert "error" not in line


@pytest.mark.parametrize(
    ("prompt", "env", "key"),
    [
        ("Analyse SHOP-412, it blocks PROJ-7", {}, "SHOP-412"),
        ("Analyse SHOP-412", {"HARNESS_TICKET": "PROJ-1"}, "PROJ-1"),
        ("Analyse SHOP-412", {"HARNESS_TICKET": "../escape"}, "SHOP-412"),
        ("Analyse shop-412", {}, "adhoc-20260304-a998c90c"),
        ("Tidy the feed code", {}, "adhoc-20260304-a998c90c"),
    ],
)
def test_the_ticket_key_comes_from_the_task_prompt(tmp_path, prompt, env, key):
    event = stop(tmp_path, agent="agent-harness:jira-analyst", agent_id="a998c90c8964cba73")
    path = transcript(
        tmp_path,
        user("<system-reminder>OTHER-9</system-reminder>", isMeta=True),
        user(prompt),
        user("Also look at OTHER-8", stamp="2026-03-05T10:00:00Z"),
        said("Done."),
        name="agent-a998c90c8964cba73",
    )
    event["agent_transcript_path"] = str(path)

    tickets = save(tmp_path, event, **env)

    assert [p.name for p in tickets.iterdir()] == [key]


@pytest.mark.parametrize(
    "agent", [None, "Explore", "general-purpose", "agent-harness:other", "estimator-x"]
)
def test_other_agents_leave_no_artifacts(tmp_path, agent):
    assert not save(tmp_path, stop(tmp_path, agent=agent)).exists()


@pytest.mark.parametrize(
    "agent",
    ["jira-analyst", "estimator", "implementer", "reviewer-qa", "agent-harness:estimator"],
)
def test_each_harness_agent_is_saved_under_its_short_name(tmp_path, agent):
    ticket = save(tmp_path, stop(tmp_path, agent=agent)) / "SHOP-412"
    assert index(ticket)[0]["report"] == f"{agent.split(':')[-1]}.01.md"


def test_without_harness_home_artifacts_go_to_the_user_directory(tmp_path):
    result = hook("save_artifacts.py", stop(tmp_path), HOME=str(tmp_path), HARNESS_HOME="")
    assert result.returncode == 0
    assert (tmp_path / ".claude/agent-harness/tickets/SHOP-412/reviewer-qa.01.md").is_file()


def report_of(tmp_path: Path, *entries: dict[str, Any], **fields: Any) -> str:
    event = stop(tmp_path, **fields)
    event["agent_transcript_path"] = str(transcript(tmp_path, user("Review SHOP-412"), *entries))
    return (save(tmp_path, event) / "SHOP-412" / "reviewer-qa.01.md").read_text()


def test_a_report_handed_back_wins_over_the_last_text(tmp_path):
    entries = (said("Let me check."), handed_back("Final report"))
    report = report_of(tmp_path, *entries, last_assistant_message="Let me check.")
    assert report == "Final report\n"


def test_text_after_a_hand_back_is_the_report(tmp_path):
    entries = (handed_back("First report"), said("Fixed, final report", "m9"))
    assert report_of(tmp_path, *entries) == "Fixed, final report\n"


def test_the_last_assistant_message_of_the_event_wins_over_the_transcript_text(tmp_path):
    report = report_of(tmp_path, said("Older text"), last_assistant_message="Final text")
    assert report == "Final text\n"


def test_a_final_message_split_over_entries_is_joined(tmp_path):
    entries = (said("Let me look.", "m1"), said("Part one", "m2"), said("Part two", "m2"))
    assert report_of(tmp_path, *entries) == "Part one\n\nPart two\n"


def test_without_a_transcript_the_event_message_is_saved_under_an_adhoc_key(tmp_path):
    event = stop(tmp_path, agent_transcript_path="", last_assistant_message="Report")
    day = datetime.now(UTC).strftime("%Y%m%d")

    [ticket] = save(tmp_path, event).iterdir()

    assert ticket.name == f"adhoc-{day}-a1"
    assert (ticket / "reviewer-qa.01.md").read_text() == "Report\n"


def test_a_run_without_a_report_leaves_nothing(tmp_path):
    event = stop(tmp_path)
    event["agent_transcript_path"] = str(transcript(tmp_path, user("Review SHOP-412")))
    assert not save(tmp_path, event).exists()


def test_every_block_is_extracted_and_invalid_ones_are_recorded(tmp_path):
    report = "\n\n".join(
        [
            "Analysis done.",
            fenced("analysis", ANALYSIS),
            fenced("review", '{"verdict": "pass",}'),
            fenced("estimate", [1, 2]),
            fenced("gates", {"passed": True}),
            "  " + fenced("implementation", {"files": []}).replace("\n", "\n  "),
            '```json\n{"plain": true}\n```',
        ]
    )

    ticket = save(tmp_path, stop(tmp_path, report, agent="jira-analyst")) / "SHOP-412"

    [line] = index(ticket)
    assert line["blocks"] == ["analysis", "implementation"]
    assert sorted(line["error"]) == ["estimate", "gates", "review"]
    assert line["error"]["review"].startswith("invalid JSON")
    assert line["error"]["estimate"] == "not a JSON object"
    assert read(ticket / "analysis.json") == ANALYSIS
    assert read(ticket / "implementation.json") == {"files": []}
    assert not (ticket / "review.json").exists()
    assert not (ticket / "estimate.json").exists()
    assert not (ticket / "gates.json").exists()


def test_earlier_blocks_are_kept_as_numbered_versions(tmp_path):
    for round_ in (1, 2, 3):
        report = fenced("review", {**REVIEW, "round": round_})
        tickets = save(tmp_path, stop(tmp_path, report, agent_id=f"r{round_}"))
    ticket = tickets / "SHOP-412"

    assert read(ticket / "review.json")["round"] == 3
    assert read(ticket / "review.01.json")["round"] == 1
    assert read(ticket / "review.02.json")["round"] == 2
    assert [line["report"] for line in index(ticket)] == [
        "reviewer-qa.01.md",
        "reviewer-qa.02.md",
        "reviewer-qa.03.md",
    ]


def test_two_blocks_of_one_kind_in_a_report_keep_both(tmp_path):
    report = fenced("review", {"n": 1}) + "\n\n" + fenced("review", {"n": 2})

    ticket = save(tmp_path, stop(tmp_path, report)) / "SHOP-412"

    assert read(ticket / "review.json") == {"n": 2}
    assert read(ticket / "review.01.json") == {"n": 1}
    assert index(ticket)[0]["blocks"] == ["review"]


def test_reports_are_numbered_per_agent(tmp_path):
    for agent, agent_id in (("jira-analyst", "a1"), ("implementer", "i1"), ("jira-analyst", "a2")):
        tickets = save(tmp_path, stop(tmp_path, agent=agent, agent_id=agent_id))

    names = sorted(p.name for p in (tickets / "SHOP-412").glob("*.md"))

    assert names == ["implementer.01.md", "jira-analyst.01.md", "jira-analyst.02.md"]


def test_a_stop_after_a_blocked_stop_replaces_the_run(tmp_path):
    first = fenced("implementation", {"files": ["a.py"]})
    second = fenced("implementation", {"files": ["a.py", "b.py"]})
    save(tmp_path, stop(tmp_path, f"Done.\n{first}", agent="implementer"))
    save(tmp_path, stop(tmp_path, "Unrelated.", agent="implementer", agent_id="other"))

    blocked = stop(tmp_path, f"Fixed.\n{second}", agent="implementer", stop_hook_active=True)
    ticket = save(tmp_path, blocked) / "SHOP-412"

    lines = index(ticket)
    assert [(line["agent_id"], line["report"]) for line in lines] == [
        ("a1", "implementer.01.md"),
        ("other", "implementer.02.md"),
    ]
    assert lines[0]["blocks"] == ["implementation"]
    assert (ticket / "implementer.01.md").read_text().startswith("Fixed.")
    assert read(ticket / "implementation.json") == {"files": ["a.py", "b.py"]}
    assert read(ticket / "implementation.01.json") == {"files": ["a.py"]}


def test_a_resumed_agent_adds_a_run(tmp_path):
    save(tmp_path, stop(tmp_path, "Round 1", agent="implementer"))
    ticket = save(tmp_path, stop(tmp_path, "Round 2", agent="implementer")) / "SHOP-412"

    assert [line["report"] for line in index(ticket)] == [
        "implementer.01.md",
        "implementer.02.md",
    ]


def test_a_blocked_stop_without_an_earlier_run_adds_one(tmp_path):
    event = stop(tmp_path, agent="implementer", stop_hook_active=True)
    ticket = save(tmp_path, event) / "SHOP-412"
    assert [line["report"] for line in index(ticket)] == ["implementer.01.md"]


@pytest.mark.parametrize("stdin", ["not json", "[]", "null", "{}"])
def test_bad_input_never_fails(tmp_path, stdin):
    result = subprocess.run(  # noqa: S603
        [sys.executable, str(HOOKS / "save_artifacts.py")],
        input=stdin,
        capture_output=True,
        text=True,
        env={**os.environ, "HARNESS_HOME": str(tmp_path)},
        check=False,
    )
    assert result.returncode == 0
    assert list(tmp_path.iterdir()) == []


def test_an_unwritable_home_never_fails(tmp_path):
    (tmp_path / "home").write_text("a file, not a directory")
    result = hook("save_artifacts.py", stop(tmp_path), HARNESS_HOME=str(tmp_path / "home"))
    assert result.returncode == 0
    assert result.stderr.startswith("save_artifacts:")
    message = json.loads(result.stdout)["systemMessage"]
    assert message.startswith("agent-harness could not save the reviewer-qa report: ")
    assert "is not writable" in message


@pytest.mark.parametrize(
    "prompt",
    [
        "Retro for SHOP-412. Do not write yet.",
        "  retro FOR SHOP-412",
        "SHOP-412: Retro for SHOP-412. Do not write yet.",
        "SHOP-412 : retro for SHOP-412",
    ],
)
def test_a_retro_run_saves_nothing(tmp_path, prompt):
    report = f"Lessons.\n\n{fenced('review', REVIEW)}"
    tickets = save(tmp_path, stop(tmp_path, report, prompt=prompt))
    assert not tickets.exists()


@pytest.mark.parametrize(
    "prompt", ["SHOP-412: Review the retro for SHOP-411", "Explain the Retro format", "SHOP-412:"]
)
def test_a_task_that_only_mentions_a_retro_is_saved(tmp_path, prompt):
    tickets = save(tmp_path, stop(tmp_path, "Done.", prompt=prompt))
    assert tickets.exists()


@pytest.mark.parametrize(
    ("prompt", "block", "key"),
    [
        ("Use UTF-8 and ISO-8601 dates in SHOP-412", {}, "SHOP-412"),
        ("Follow PSR-12, RFC-7231 and HTTP-2 for SHOP-412", {}, "SHOP-412"),
        ("Check SHA-256 sums", {}, "adhoc-20260304-a1"),
        ("Review SHOP-412", {"ticket": "SHOP-500"}, "SHOP-500"),
        ("Review SHOP-412", {"ticket": " SHOP-500 "}, "SHOP-500"),
        ("Review SHOP-412", {"ticket": "UTF-8"}, "SHOP-412"),
        ("Review SHOP-412", {"ticket": "../escape"}, "SHOP-412"),
        ("Review SHOP-412", {"ticket": 7}, "SHOP-412"),
    ],
)
def test_the_key_skips_standards_and_prefers_the_reports_block(tmp_path, prompt, block, key):
    review = {name: value for name, value in REVIEW.items() if name != "ticket"}
    report = f"Verdict.\n\n{fenced('review', {**review, **block})}"
    tickets = save(tmp_path, stop(tmp_path, report, prompt=prompt))
    assert [path.name for path in tickets.iterdir()] == [key]


def test_the_ticket_override_still_wins_over_the_block(tmp_path):
    report = f"Verdict.\n\n{fenced('review', {**REVIEW, 'ticket': 'SHOP-500'})}"
    tickets = save(tmp_path, stop(tmp_path, report), HARNESS_TICKET="PROJ-1")
    assert [path.name for path in tickets.iterdir()] == ["PROJ-1"]


@pytest.mark.parametrize(
    ("agent", "kind", "keys", "items", "item_keys"),
    [
        (
            "jira-analyst",
            "analysis",
            {"ticket", "repositories", "plan"},
            "plan",
            {"repo", "file", "change", "tests"},
        ),
        (
            "reviewer-qa",
            "review",
            {"ticket", "verdict", "findings"},
            "findings",
            {"id", "severity", "file", "line", "summary", "evidence", "disposition"},
        ),
        (
            "implementer",
            "implementation",
            {"ticket", "files", "checks", "time_sinks"},
            "checks",
            {"cmd", "exit"},
        ),
    ],
)
def test_the_example_block_in_each_agent_prompt_follows_the_contract(
    tmp_path, agent, kind, keys, items, item_keys
):
    prompt = (HOOKS.parent / "agents" / f"{agent}.md").read_text()

    ticket = save(tmp_path, stop(tmp_path, prompt, agent=agent)) / "SHOP-412"

    assert index(ticket)[0]["blocks"] == [kind]
    block = read(ticket / f"{kind}.json")
    assert set(block) == keys
    assert {key for item in block[items] for key in item} == item_keys
