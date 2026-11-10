import itertools
import json
from dataclasses import asdict, replace
from pathlib import Path

import pytest
from ticket_precedent.config import Config, EmbeddingConfig, SourceConfig
from ticket_precedent.search import Searcher

from agent_harness import cli
from agent_harness.baselines import Estimate, HumanEstimate, NeighbourMedian
from agent_harness.dataset import load_targets
from agent_harness.llm import LLMError, LLMEstimator
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
        "manifest.json",
        "neighbour-median.jsonl",
        "recent-median.jsonl",
        "era-scaled-median.jsonl",
        "human.jsonl",
    }

    assert cli.main(["report", str(out)]) == 0
    assert "| recent-median |" in capsys.readouterr().out


@pytest.fixture
def no_user_config(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    monkeypatch.delenv("TICKET_PRECEDENT_CONFIG", raising=False)
    return tmp_path / "xdg-config" / "ticket-precedent" / "config.yaml"


def human_run(out, *extra):
    base = ["run", "--out", str(out), "--from", "2026-01-01", "--to", "2027-01-01"]
    return cli.main([*base, "--variants", "human", *extra])


def test_cli_run_refuses_a_named_config_that_does_not_exist(
    no_user_config, tmp_path, monkeypatch, capsys
):
    missing = tmp_path / "missing.yaml"

    assert human_run(tmp_path / "by-option", "--config", str(missing)) == 1
    assert f"--config names {missing}, which does not exist" in capsys.readouterr().err
    monkeypatch.setenv("TICKET_PRECEDENT_CONFIG", str(missing))
    assert human_run(tmp_path / "by-env") == 1
    assert f"TICKET_PRECEDENT_CONFIG names {missing}" in capsys.readouterr().err
    assert not (tmp_path / "by-option").exists()
    assert not (tmp_path / "by-env").exists()


def test_cli_run_without_a_named_config_uses_the_default_path(
    no_user_config, store, tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr("agent_harness.cli.build_searcher", lambda config, store: _searcher(store))

    assert human_run(tmp_path / "no-config") == 0
    assert "platform all: 0 tickets" in capsys.readouterr().err

    no_user_config.parent.mkdir(parents=True)
    no_user_config.write_text(f"index_path: {store.path}\nsources: []\n")
    assert human_run(tmp_path / "default-file") == 0
    assert "platform all: 4 tickets" in capsys.readouterr().err

    monkeypatch.setenv("TICKET_PRECEDENT_CONFIG", str(no_user_config))
    assert human_run(tmp_path / "by-env") == 0
    assert "platform all: 4 tickets" in capsys.readouterr().err


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


SHOP_TARGETS = {"SHOP-4", "SHOP-5", "SHOP-6", "OPS-1", "HD-2"}
SHOP_CITED = {"SHOP-4", "SHOP-5", "OPS-1", "HD-2"}


def platform_config(store, default="shop", platforms=("shop", "shop", "legacy")):
    names = ("acme-jira", "acme-hd", "legacy-jira")
    sources = [
        SourceConfig(name=name, type="jira", options={}, platform=platform)
        for name, platform in zip(names, platforms, strict=True)
    ]
    return Config(
        index_path=store.path,
        embedding=EmbeddingConfig(),
        sources=sources,
        default_platform=default,
    )


@pytest.fixture
def run_on(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("agent_harness.cli.build_searcher", lambda config, store: _searcher(store))
    runs = itertools.count()

    def go(config, *extra, out=None):
        monkeypatch.setattr("agent_harness.cli.load_config", lambda path: config)
        out = out or tmp_path / f"run-{next(runs)}"
        base = ["run", "--out", str(out), "--from", "2026-01-01", "--to", "2027-01-01"]
        code = cli.main([*base, "--variants", "recent-median", *extra])
        err = capsys.readouterr().err
        if code != 0:
            return code, err, []
        return code, err, load_records(run_file(out, "recent-median"))

    return go


@pytest.mark.parametrize(
    ("default", "extra", "line", "keys", "cited"),
    [
        ("shop", [], "platform shop: 5 tickets", SHOP_TARGETS, SHOP_CITED),
        (None, ["--platform", "shop"], "platform shop: 5 tickets", SHOP_TARGETS, SHOP_CITED),
        ("shop", ["--platform", "legacy"], "platform legacy: 1 tickets", {"LEG-2"}, set()),
        (
            "shop",
            ["--platform", "all"],
            "platform all: 6 tickets",
            SHOP_TARGETS | {"LEG-2"},
            SHOP_CITED | {"LEG-2"},
        ),
    ],
)
def test_cli_run_keeps_targets_and_neighbours_in_one_platform(
    run_on, mixed_store, default, extra, line, keys, cited
):
    code, err, records = run_on(platform_config(mixed_store, default), *extra)

    assert code == 0
    assert line in err
    assert {record.id.split("/")[1] for record in records} == keys
    assert {key for record in records for key in record.cited} == cited


def test_cli_run_rejects_an_unknown_platform(run_on, mixed_store):
    code, err, _ = run_on(platform_config(mixed_store), "--platform", "nope")

    assert code == 1
    assert "Unknown platform 'nope'; configured: ['legacy', 'shop']" in err


def test_cli_run_does_not_mix_platforms_unless_asked(run_on, mixed_store):
    code, err, _ = run_on(platform_config(mixed_store, default=None))

    assert code == 1
    assert "No platform chosen" in err
    assert "--platform" in err


def test_cli_run_without_platforms_uses_every_source(run_on, mixed_store):
    code, err, records = run_on(platform_config(mixed_store, None, platforms=(None, None, None)))

    assert code == 0
    assert "platform all: 6 tickets" in err
    assert len(records) == 6


def test_a_run_writes_its_manifest_and_resumes_under_it(run_on, mixed_store, tmp_path):
    out = tmp_path / "resumed"
    config = platform_config(mixed_store)

    first_code, _, first_records = run_on(config, out=out)
    manifest = json.loads((out / "manifest.json").read_text())
    code, err, records = run_on(config, out=out)

    assert first_code == 0
    assert manifest == {
        "platform": "shop",
        "sources": ["acme-hd", "acme-jira"],
        "index_path": str(mixed_store.path.resolve()),
    }
    assert code == 0
    assert "warning" not in err
    assert {(r.id, r.median) for r in records} == {(r.id, r.median) for r in first_records}


@pytest.mark.parametrize("field", ["platform", "sources", "index_path"])
def test_a_run_refuses_to_resume_under_another_platform_or_index(
    run_on, mixed_store, tmp_path, field
):
    out = tmp_path / "resumed"
    assert run_on(platform_config(mixed_store), out=out)[0] == 0
    manifest = (out / "manifest.json").read_text()
    changed = {
        "platform": (platform_config(mixed_store), ["--platform", "all"]),
        "sources": (platform_config(mixed_store, platforms=("shop", "legacy", "legacy")), []),
        "index_path": (replace(platform_config(mixed_store), index_path=tmp_path / "b.db"), []),
    }
    config, extra = changed[field]

    code, err, _ = run_on(config, *extra, out=out)

    assert code == 1
    assert f"{out} was run with {field} " in err
    assert (out / "manifest.json").read_text() == manifest


def test_a_run_directory_from_before_manifests_resumes_with_a_warning(
    run_on, mixed_store, tmp_path
):
    out = tmp_path / "old-run"
    out.mkdir()
    (out / "human.jsonl").write_text("")

    code, err, records = run_on(platform_config(mixed_store), out=out)

    assert code == 0
    assert f"warning: {out} has no manifest.json" in err
    assert len([line for line in err.splitlines() if "warning" in line]) == 1
    assert len(records) == len(SHOP_TARGETS)
    assert not (out / "manifest.json").exists()


def test_cli_gives_every_retrieving_variant_the_platform(store, tmp_path):
    variants = "neighbour-median,recent-median,era-scaled-median,llm-hybrid,human"
    args = run_args(tmp_path, "--variants", variants, "--backend", "ollama")
    sources = frozenset({"acme-jira"})

    estimators = cli.build_estimators(args, _searcher(store), sources)

    retrieving = [e for e in estimators if isinstance(e, NeighbourMedian | LLMEstimator)]
    assert len(retrieving) == 4
    assert all(estimator.sources == sources for estimator in retrieving)


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


class Rejecting:
    name = "llm-hybrid-recent-structured@fake"

    def estimate(self, target):
        if target.key == "SHOP-5":
            raise LLMError("rejected, uncited adjustment: adjustments[0] has no citation", 0.02)
        if target.key == "SHOP-6":
            raise LLMError("rejected: mode must be one of", 0.01)
        return Estimate(
            median=target.actual_hours,
            reference_median=1.0,
            lessons=1 if target.key == "SHOP-4" else 0,
            lessons_dated=3,
            lessons_undated=40,
            block_mode="reference" if target.key != "SHOP-3" else "bottom-up",
            block_reference_median=2.0 if target.key == "SHOP-2" else 1.0,
            retried=target.key in ("SHOP-1", "SHOP-2"),
            check_errors=("adjustments[0] has no citation",) if target.key == "SHOP-1" else (),
        )


def test_a_rejected_estimate_is_a_failure_that_keeps_its_cost(tmp_path, targets):
    run([Rejecting()], targets, tmp_path)

    records = {r.id: r for r in load_records(run_file(tmp_path, Rejecting.name))}

    assert records["acme-jira/SHOP-5"].error.startswith("rejected, uncited")
    assert records["acme-jira/SHOP-5"].cost_usd == 0.02
    assert records["acme-jira/SHOP-4"].reference_median == 1.0
    assert records["acme-jira/SHOP-4"].lessons == 1
    assert records["acme-jira/SHOP-1"].check_errors == ("adjustments[0] has no citation",)
    assert records["acme-jira/SHOP-2"].block_reference_median == 2.0


def test_run_notes_show_anchoring_rejections_and_memory(tmp_path, targets):
    run([Rejecting(), Flaky()], targets, tmp_path)

    notes = report(load_run(tmp_path)).split("## Run notes")[1]

    structured = next(line for line in notes.splitlines() if Rejecting.name in line)
    assert "; 20% of 5 estimates at the reference median" in structured
    assert "; 2 rejected by the estimate check, 1 of them for an uncited adjustment" in structured
    assert "; 2 retried after the check, 1 kept after the check failed (stated total)" in structured
    assert (
        "; median drift: 1 of 4 reference-mode blocks (25%) wrote a median other than the "
        "retrieved one" in structured
    )
    assert (
        "; memory: 0.2 lessons per ticket (at most 1) of 3 dated, 40 without a date left out"
        in structured
    )
    flaky = next(line for line in notes.splitlines() if line.startswith("- flaky"))
    assert "reference median" not in flaky
    assert "rejected" not in flaky
    assert "memory" not in flaky


def run_args(tmp_path, *extra):
    base = ["run", "--out", str(tmp_path), "--from", "2026-01-01", "--to", "2027-01-01"]
    return cli._parser().parse_args([*base, *extra])


def test_cli_builds_structured_and_memory_variants(store, tmp_path):
    memory = tmp_path / "memory"
    memory.mkdir()
    (memory / "MEMORY.md").write_text("- a lesson (source: retro, 2026-01-05)\n- undated\n")
    variants = "llm-hybrid-recent,llm-hybrid-recent-structured,llm-hybrid-recent-structured+memory"
    args = run_args(tmp_path, "--variants", variants, "--backend", "ollama")
    args.memory = memory

    estimators = cli.build_estimators(args, _searcher(store))

    assert [e.name for e in estimators] == [
        "llm-hybrid-recent@ollama-gemma4:12b",
        "llm-hybrid-recent-structured@ollama-gemma4:12b",
        "llm-hybrid-recent-structured+memory@ollama-gemma4:12b",
    ]
    llm = [estimator for estimator in estimators if isinstance(estimator, LLMEstimator)]
    assert len(llm) == 3
    old, structured, with_memory = llm
    assert not old.structured
    assert structured.structured
    assert structured.memory is None
    assert with_memory.memory is not None
    assert (with_memory.memory.dated, with_memory.memory.undated) == (1, 1)
    assert with_memory.since == "2026-01-01"


def test_cli_memory_errors(config_file, tmp_path, capsys):
    base = ["run", "--out", str(tmp_path / "r"), "--from", "2026-01-01", "--to", "2027-01-01"]
    base += ["--config", str(config_file), "--backend", "ollama"]

    assert cli.main([*base, "--variants", "llm-hybrid-structured+memory"]) == 1
    assert "needs --memory DIR" in capsys.readouterr().err
    missing = str(tmp_path / "missing")
    assert cli.main([*base, "--variants", "llm-hybrid+memory", "--memory", missing]) == 1
    assert "No memory directory" in capsys.readouterr().err
    assert cli.main([*base, "--variants", "llm-guess-structured"]) == 1
    assert "Unknown variant" in capsys.readouterr().err
