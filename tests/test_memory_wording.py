from pathlib import Path

import pytest

from agent_harness import llm

AGENTS = Path(__file__).resolve().parents[1] / "agents"
TEAM = ("implementer", "reviewer-qa", "jira-analyst", "estimator")


def prose(text: str) -> str:
    return " ".join(text.split())


@pytest.mark.parametrize("agent", TEAM)
def test_each_agent_says_only_the_index_is_loaded_and_how_to_write_memory(agent):
    text = prose((AGENTS / f"{agent}.md").read_text())

    assert "memory directory is loaded" not in text
    assert "Only the index of your memory, `MEMORY.md`, is loaded into this prompt" in text
    assert "the topic files it links are not" in text
    assert "Write memory only with Edit or Write, never through Bash" in text


@pytest.mark.parametrize("agent", ["implementer", "reviewer-qa", "jira-analyst"])
def test_the_read_step_opens_the_topic_files(agent):
    text = prose((AGENTS / f"{agent}.md").read_text())

    assert "Only its index is in this prompt: open the topic files it links" in text


def test_the_estimator_reads_its_calibration_and_corrections_first():
    text = prose((AGENTS / "estimator.md").read_text())

    assert "## Read your memory first" in text
    assert text.index("## Read your memory first") < text.index("## Choose the method")
    assert "open the topic files it links that hold the calibration and the corrections" in text


@pytest.mark.parametrize("offline", [llm.OFFLINE, llm.OFFLINE_STRUCTURED])
def test_the_offline_notes_say_there_are_no_memory_files_to_open(offline):
    assert "There are no memory files to open" in prose(offline)
    assert "already in this prompt" in prose(offline)
