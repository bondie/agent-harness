import itertools
import json
from dataclasses import asdict
from pathlib import Path

import pytest
from ticket_precedent.search import Searcher

from agent_harness import cli
from agent_harness.baselines import Estimate, HumanEstimate, NeighbourMedian
from agent_harness.dataset import load_targets
from agent_harness.llm import LLMError
from agent_harness.report import project_labels, report
from agent_harness.runner import Record, load_records, load_run, run, run_file

from .conftest import FakeEmbedder


class Flaky:
    name = "flaky"

    def __init__(self, fail_on=()):
        self.fail_on = set(fail_on)
        self.seen = []

    def estimate(self, target):
        self.seen.append(target.key)
        if target.key in self.fail_on:
            raise LLMError("rate limited")
        return Estimate(median=target.actual_hours + 1, p25=target.actual_hours, p75=9.0)


def ticks():
    counter = itertools.count()
    return lambda: float(next(counter))


@pytest.fixture
def targets(store):
    return load_targets(store, "2025-01-01", "2027-01-01")


def test_run_writes_one_line_per_ticket_and_resumes_after_errors(tmp_path, targets):
    first = Flaky(fail_on={"SHOP-5"})
    run([first], targets, tmp_path, clock=ticks())

    records = {r.id: r for r in load_records(run_file(tmp_path, "flaky"))}
    assert records["acme-jira/SHOP-5"].error == "rate limited"
    assert records["acme-jira/SHOP-4"].median == 2.0
    assert records["acme-jira/SHOP-4"].seconds == 1.0

    second = Flaky()
    progress = []
    run([second], targets, tmp_path, progress=lambda *args: progress.append(args))

    assert second.seen == ["SHOP-5"]
    assert progress == [("flaky", 1, 1)]
    retried = {r.id: r for r in load_records(run_file(tmp_path, "flaky"))}
    assert not retried["acme-jira/SHOP-5"].error


def test_missing_estimates_are_recorded(tmp_path, targets):
    run([HumanEstimate()], targets, tmp_path)
    records = {r.id: r for r in load_records(run_file(tmp_path, "human"))}
    assert records["acme-jira/SHOP-3"].error == "no estimate"


def test_run_file_names_are_safe(tmp_path):
    assert run_file(tmp_path, "llm-hybrid@ollama-gemma4:12b").name == (
        "llm-hybrid@ollama-gemma4_12b.jsonl"
    )
    assert load_records(tmp_path / "missing.jsonl") == []


def test_report_is_aggregate_only(tmp_path, store, searcher, targets):
    run([NeighbourMedian(searcher), HumanEstimate(), Flaky()], targets, tmp_path)

    text = report(load_run(tmp_path))

    assert "## All variants on the tickets every variant estimated" in text
    assert "| neighbour-median | 6 |" in text
    assert "| flaky | 6 |" in text
    assert "## With the human estimate" in text
    assert "## Tickets whose logged time differs from the human estimate" in text
    assert "### Project A" in text
    assert "Compared on 6 tickets" in text
    for private in ("SHOP-", "OPS-", "acme", "SHOP |", "Price export"):
        assert private not in text


def test_report_with_too_few_tickets(tmp_path):
    record = Record("neighbour-median", "x/1", "P", "Bug", "2026-01-01", 1.0, None, 1.0)
    path = run_file(tmp_path, "neighbour-median")
    path.write_text(json.dumps(asdict(record)) + "\n")
    assert "Fewer than two tickets" in report(load_run(tmp_path))


def test_project_labels_order_by_size():
    def record(project):
        return Record("v", project + "/1", project, "Bug", "2026", 1.0, None, 1.0)

    labels = project_labels([record("small"), record("big"), record("big")])
    assert labels == {"big": "Project A", "small": "Project B"}


@pytest.fixture
def config_file(tmp_path: Path, store, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text(f"index_path: {store.path}\nsources: []\n")
    monkeypatch.setattr("agent_harness.cli.build_searcher", lambda config, store: _searcher(store))
    return path


def _searcher(store):
    return Searcher(store, FakeEmbedder())


def test_cli_run_and_report(config_file, tmp_path, capsys):
    out = tmp_path / "run"
    arguments = ["run", "--out", str(out), "--from", "2026-01-01", "--to", "2027-01-01"]
    assert cli.main([*arguments, "--config", str(config_file)]) == 0
    assert "4 tickets" in capsys.readouterr().err
    assert {p.name for p in out.iterdir()} == {
        "neighbour-median.jsonl",
        "recent-median.jsonl",
        "era-scaled-median.jsonl",
        "human.jsonl",
    }

    assert cli.main(["report", str(out)]) == 0
    assert "| recent-median |" in capsys.readouterr().out


def test_cli_errors(config_file, tmp_path, capsys):
    base = ["run", "--out", str(tmp_path / "r"), "--from", "2026-01-01", "--to", "2027-01-01"]
    base += ["--config", str(config_file)]

    assert cli.main([*base, "--variants", "llm-hybrid"]) == 1
    assert "needs at least one --backend" in capsys.readouterr().err
    assert cli.main([*base, "--variants", "guess"]) == 1
    assert "Unknown variant" in capsys.readouterr().err
    assert cli.main([*base, "--variants", "llm-none", "--backend", "gpt:4"]) == 1
    assert "Unknown backend" in capsys.readouterr().err
    assert cli.main(["report", str(tmp_path / "empty")]) == 1
    assert "No results" in capsys.readouterr().err


def test_cli_builds_llm_variants_for_every_backend(store, tmp_path):
    args = cli._parser().parse_args(
        [
            "run",
            "--out",
            str(tmp_path),
            "--from",
            "2026-01-01",
            "--to",
            "2027-01-01",
            "--variants",
            "llm-keyword,llm-hybrid-recent,human",
            "--backend",
            "claude:haiku",
            "--backend",
            "ollama",
        ]
    )
    names = [e.name for e in cli.build_estimators(args, _searcher(store))]
    assert names == [
        "llm-keyword@claude-haiku",
        "llm-keyword@ollama-gemma4:12b",
        "llm-hybrid-recent@claude-haiku",
        "llm-hybrid-recent@ollama-gemma4:12b",
        "human",
    ]
