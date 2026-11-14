import importlib
import json
from datetime import datetime
from pathlib import Path

import pytest
import yaml

from agent_harness import cli
from agent_harness.artifacts import (
    IncidentCase,
    append_escaped,
    harness_home,
    incident_case,
    report_blocks,
    set_disposition,
    ticket_dir,
    ticket_dirs,
)
from agent_harness.gitlog import changes, diff

HOOKS = Path(__file__).resolve().parents[1] / "hooks"

ESTIMATE = {
    "ticket": "SHOP-412",
    "mode": "bottom-up",
    "reference_median": None,
    "parts": [
        {"name": "rounding", "kind": "implementation", "hours": 1.0, "evidence": ["SHOP-1"]},
        {"name": "checkout", "kind": "verification", "hours": 0.5, "evidence": ["SHOP-2"]},
        {"name": "deploy", "kind": "release", "hours": 0.5, "evidence": ["measured:deploy"]},
    ],
    "adjustments": [],
    "total": 2.0,
}
ANALYSIS = {
    "ticket": "SHOP-412",
    "repositories": ["shop"],
    "plan": [{"repo": "shop", "file": "src/Price.php", "change": "round", "tests": True}],
}
REVIEW = {
    "ticket": "SHOP-412",
    "verdict": "pass",
    "findings": [
        {"id": "F1", "severity": "major", "file": "src/Price.php", "line": 12, "disposition": None},
        {"id": "F2", "severity": "minor", "file": "src/Price.php", "line": 30, "disposition": None},
    ],
}


@pytest.fixture
def home(tmp_path, monkeypatch):
    path = tmp_path / "harness"
    monkeypatch.setenv("HARNESS_HOME", str(path))
    monkeypatch.setenv("HARNESS_RUNS_LOG", str(tmp_path / "runs.jsonl"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    ticket = path / "tickets" / "SHOP-412"
    ticket.mkdir(parents=True)
    (ticket / "estimate.json").write_text(json.dumps(ESTIMATE))
    (ticket / "analysis.json").write_text(json.dumps(ANALYSIS))
    (ticket / "review.json").write_text(json.dumps(REVIEW))
    return path


def read(path: Path):
    return json.loads(path.read_text())


def test_harness_home_defaults_to_the_run_log_directory(monkeypatch):
    monkeypatch.delenv("HARNESS_HOME", raising=False)
    assert harness_home() == Path.home() / ".claude" / "agent-harness"


@pytest.mark.parametrize(
    "key", ["../etc", "a/b", "", ".hidden", "SHOP-1/..", "UTF-8", "PSR-12", "SHA-256", "utf-8"]
)
def test_ticket_dir_rejects_unsafe_keys(key, tmp_path):
    with pytest.raises(ValueError, match="Not a ticket key"):
        ticket_dir(key, tmp_path)


def test_ticket_dirs_without_tickets(tmp_path):
    assert ticket_dirs(tmp_path) == []


def test_export_case_with_the_diff_from_git(home, git_repo, capsys):
    git_repo.commit("init", {"src/Price.php": "<?php\n"})
    git_repo.commit(
        "fix: round prices - SHOP-412",
        {"src/Price.php": "<?php\n// a\n// b\n", "tests/PriceTest.php": "<?php\n"},
    )
    arguments = ["export-case", "SHOP-412", "--source", "measured", "--repo", str(git_repo.path)]
    arguments += ["--actual-part", "implementation=1.5", "verification=0.5", "release=0.25"]

    assert cli.main(arguments) == 0

    assert "SHOP-412: estimate 2 h, actual 2.25 h" in capsys.readouterr().out
    case = read(home / "eval-cases" / "SHOP-412.json")
    assert case == {
        "ticket": "SHOP-412",
        "estimate": ESTIMATE,
        "analysis": ANALYSIS,
        "actual": {
            "total": 2.25,
            "parts": {"implementation": 1.5, "verification": 0.5, "release": 0.25},
            "source": "measured",
        },
        "diff": {
            "files": ["src/Price.php", "tests/PriceTest.php"],
            "added": 3,
            "removed": 0,
            "test_added": 1,
        },
    }
    outcome = read(home / "tickets" / "SHOP-412" / "outcome.json")
    assert outcome["actual"] == case["actual"]
    assert outcome["diff"] == case["diff"]
    datetime.fromisoformat(outcome["exported_at"])


def test_export_case_with_custom_test_patterns_and_grep(home, git_repo, capsys):
    git_repo.commit("old key PROJ-7", {"e2e/cart.cy.ts": "x\n"})
    arguments = ["export-case", "SHOP-412", "--source", "worklog", "--actual-total", "3"]
    arguments += ["--repo", str(git_repo.path), "--grep", "PROJ-7", "--test-pattern", "e2e/"]

    assert cli.main(arguments) == 0

    case = read(home / "eval-cases" / "SHOP-412.json")
    assert case["diff"]["test_added"] == 1
    assert case["actual"] == {"total": 3.0, "parts": {}, "source": "worklog"}


def test_export_case_without_repositories_or_commits(home, git_repo, capsys):
    git_repo.commit("init", {"a": "a"})
    arguments = ["export-case", "SHOP-412", "--source", "measured", "--actual-total", "2"]
    assert cli.main(arguments) == 0
    assert read(home / "eval-cases" / "SHOP-412.json")["diff"] is None

    assert cli.main([*arguments, "--repo", str(git_repo.path)]) == 0
    assert "no commit names SHOP-412" in capsys.readouterr().err
    assert read(home / "eval-cases" / "SHOP-412.json")["diff"]["files"] == []


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ([], "Give the actual time"),
        (["--actual-part", "implementation"], "KIND=HOURS"),
        (["--actual-part", "implementation=nan"], "zero or more"),
        (["--actual-total", "-2"], "zero or more"),
    ],
)
def test_export_case_errors(home, capsys, extra, message):
    assert cli.main(["export-case", "SHOP-412", "--source", "measured", *extra]) == 1
    assert message in capsys.readouterr().err


def test_export_case_needs_an_estimate(home, capsys):
    assert cli.main(["export-case", "SHOP-9", "--source", "measured", "--actual-total", "1"]) == 1
    assert "No estimate.json" in capsys.readouterr().err


def test_eval_reads_the_exported_cases(home, capsys):
    arguments = ["export-case", "SHOP-412", "--source", "measured", "--actual-total", "3"]
    assert cli.main(arguments) == 0
    capsys.readouterr()

    assert cli.main(["eval"]) == 0
    text = capsys.readouterr().out
    assert "| all | 1 | 1.00 | 1.00 | -1.00 | 0 | 1 |" in text
    assert "| bottom-up | 1 |" in text
    assert "| implementation | 0 | - |" in text
    assert "SHOP-412" not in text

    assert cli.main(["eval", str(home / "nothing")]) == 1
    assert "No eval cases" in capsys.readouterr().err


def test_review_finding_records_the_disposition(home, capsys):
    arguments = ["review-finding", "SHOP-412", "F1", "F2", "--disposition", "rejected"]
    assert cli.main([*arguments, "--note", "not a bug"]) == 0
    assert "SHOP-412 F2: rejected" in capsys.readouterr().out
    findings = read(home / "tickets" / "SHOP-412" / "review.json")["findings"]
    assert [(f["disposition"], f["note"]) for f in findings] == [("rejected", "not a bug")] * 2

    assert cli.main(["review-finding", "SHOP-412", "F1", "--disposition", "accepted"]) == 0
    findings = read(home / "tickets" / "SHOP-412" / "review.json")["findings"]
    assert findings[0]["disposition"] == "accepted"
    assert findings[0]["note"] == "not a bug"


def test_review_finding_in_the_rounds_of_a_review(home, capsys):
    ticket = home / "tickets" / "SHOP-412"
    first = {**REVIEW, "verdict": "changes"}
    (ticket / "review.01.json").write_text(json.dumps(first))
    later = {**REVIEW, "findings": [*REVIEW["findings"], {"id": "F3", "severity": "minor"}]}
    (ticket / "review.json").write_text(json.dumps(later))

    assert cli.main(["review-finding", "SHOP-412", "F3", "--disposition", "deferred"]) == 0
    assert read(ticket / "review.json")["findings"][2]["disposition"] == "deferred"
    capsys.readouterr()

    assert cli.main(["review-finding", "SHOP-412", "F1", "--disposition", "accepted"]) == 1
    assert "Rounds 1, 2 all have F1; give --round N" in capsys.readouterr().err
    arguments = ["review-finding", "SHOP-412", "F1", "--disposition", "accepted", "--round", "1"]
    assert cli.main(arguments) == 0
    assert read(ticket / "review.01.json")["findings"][0]["disposition"] == "accepted"
    assert read(ticket / "review.json")["findings"][0]["disposition"] is None
    assert cli.main([*arguments[:-1], "3"]) == 1
    assert "No review round 3" in capsys.readouterr().err

    assert cli.main(["metrics", "--json"]) == 0
    reviewer = json.loads(capsys.readouterr().out)["reviewer"]
    assert reviewer["tickets"][0]["rounds"] == 2
    assert reviewer["tickets"][0]["verdict"] == "pass"
    assert reviewer["by_severity"]["all"]["findings"] == 5
    assert reviewer["by_severity"]["all"]["by_disposition"]["accepted"] == 1
    assert reviewer["by_severity"]["all"]["by_disposition"]["deferred"] == 1


def test_review_finding_errors(home, capsys):
    assert cli.main(["review-finding", "SHOP-412", "F9", "--disposition", "accepted"]) == 1
    error = capsys.readouterr().err
    assert "No finding F9" in error
    assert "it has: F1, F2" in error
    assert cli.main(["review-finding", "SHOP-9", "F1", "--disposition", "accepted"]) == 1
    assert "No review.json" in capsys.readouterr().err
    with pytest.raises(ValueError, match="Disposition"):
        set_disposition(home / "tickets" / "SHOP-412", ["F1"], "maybe")


def test_escaped_from_the_commit_message(home, git_repo, capsys):
    sha = git_repo.commit("fix: round prices - SHOP-412", {"a": "a"})
    arguments = ["escaped", "--commit", sha[:10], "--repo", str(git_repo.path)]
    arguments += ["--source", "sentry", "--ref", "4711", "--summary", "Price  off\nby a cent"]

    assert cli.main(arguments) == 0
    assert "SHOP-412 (from the commit message)" in capsys.readouterr().out
    assert cli.main(arguments) == 0
    assert "already recorded" in capsys.readouterr().out

    entries = read(home / "tickets" / "SHOP-412" / "escaped.json")
    assert len(entries) == 1
    entry = entries[0]
    assert {k: v for k, v in entry.items() if k != "recorded_at"} == {
        "commit": sha,
        "ticket": "SHOP-412",
        "source": "sentry",
        "ref": "4711",
        "summary": "Price off by a cent",
    }
    datetime.fromisoformat(entry["recorded_at"])


def test_escaped_from_an_incident_scout_case(home, git_repo, tmp_path, capsys):
    sha = git_repo.commit("fix: round prices - SHOP-412", {"a": "a"})
    case = tmp_path / "incident-12-http-5xx.yaml"
    document = {
        "id": "incident-12-http-5xx",
        "note": "A release calls a method that no longer exists.",
        "expect": {"category": "application_error", "scope": "single_shop", "cause": [["x"]]},
        "pack": {"rule": "http-5xx", "summary": "prod-shop-a: 1530x HTTP 5xx", "sections": []},
    }
    case.write_text(yaml.safe_dump(document))
    arguments = ["escaped", "--commit", sha, "--repo", str(git_repo.path), "--case", str(case)]

    assert cli.main(arguments) == 0

    entry = read(home / "tickets" / "SHOP-412" / "escaped.json")[0]
    assert entry["source"] == "incident-scout"
    assert entry["ref"] == "incident-12-http-5xx"
    assert entry["summary"] == "A release calls a method that no longer exists."


def test_escaped_needs_a_ticket(home, git_repo, capsys):
    sha = git_repo.commit("fix rounding", {"a": "a"})
    base = ["escaped", "--commit", sha, "--repo", str(git_repo.path)]
    assert cli.main(base) == 1
    assert "give --ticket KEY" in capsys.readouterr().err

    assert cli.main([*base, "--ticket", "SHOP-412"]) == 0
    assert "SHOP-412 (from --ticket)" in capsys.readouterr().out
    entry = read(home / "tickets" / "SHOP-412" / "escaped.json")[0]
    assert (entry["source"], entry["ref"], entry["summary"]) == ("manual", None, "")


def test_escaped_skips_the_names_of_standards(home, git_repo, capsys):
    sha = git_repo.commit("fix: read the feed as UTF-8 per PSR-12 - SHOP-412", {"a": "a"})
    assert cli.main(["escaped", "--commit", sha, "--repo", str(git_repo.path)]) == 0
    assert "SHOP-412 (from the commit message)" in capsys.readouterr().out


def test_escaped_prefers_the_key_with_a_ticket_directory(home, git_repo, capsys):
    sha = git_repo.commit("fix: SHOP-7 regression, see SHOP-412", {"a": "a"})
    assert cli.main(["escaped", "--commit", sha, "--repo", str(git_repo.path)]) == 0
    assert "SHOP-412 (from the commit message)" in capsys.readouterr().out

    other = git_repo.commit("fix: SHOP-8 and SHOP-9 together", {"b": "b"})
    assert cli.main(["escaped", "--commit", other, "--repo", str(git_repo.path)]) == 1
    assert "SHOP-8, SHOP-9; give --ticket KEY" in capsys.readouterr().err
    (home / "tickets" / "SHOP-8").mkdir()
    (home / "tickets" / "SHOP-9").mkdir()
    assert cli.main(["escaped", "--commit", other, "--repo", str(git_repo.path)]) == 1
    assert "SHOP-8, SHOP-9; give --ticket KEY" in capsys.readouterr().err


def test_metrics_reads_each_repository_once_with_the_same_files(home, git_repo, tmp_path):
    second = type(git_repo)(tmp_path / "second")
    for index, key in enumerate(["SHOP-1", "SHOP-12", "SHOP-412", "SHOP-1", "PROJ-7"]):
        git_repo.commit(f"change {key}", {f"src/{index}.php": "x"})
        second.commit(f"other {key} SHOP-412", {f"lib/{index}.ts": "y"})
    keys = ["SHOP-1", "SHOP-12", "SHOP-412", "PROJ-7", "SHOP-5"]
    repos = [git_repo.path, second.path]

    per_key = {
        key: list(diff([c for repo in repos for c in changes(repo, key)]).files) for key in keys
    }
    assert cli.files_by_key(repos, keys, None) == per_key
    assert per_key["SHOP-5"] == []


def test_export_case_keeps_an_estimate_the_check_rejected(home, capsys):
    rejected = {
        **ESTIMATE,
        "adjustments": [{"hours": 1.0, "reason": "gut feeling", "citation": ""}],
        "total": 3.0,
    }
    (home / "tickets" / "SHOP-412" / "estimate.json").write_text(json.dumps(rejected))
    arguments = ["export-case", "SHOP-412", "--source", "worklog", "--actual-total", "2"]

    assert cli.main(arguments) == 0
    assert "estimate 3 h (stated total, the estimate check failed)" in capsys.readouterr().out
    case = read(home / "eval-cases" / "SHOP-412.json")
    assert "citation" in case["check_errors"][0]

    assert cli.main(["eval"]) == 0
    text = capsys.readouterr().out
    assert "| all | 0 | - |" in text
    assert "| kept after the check failed | 1 | 1.00 | 1.00 | +1.00 | 1 | 0 |" in text
    assert "1 cases, 1 of them kept after the estimate check failed" in text


def test_export_case_on_one_branch(home, git_repo, capsys):
    git_repo.commit("init", {"src/Price.php": "a\n"})
    git_repo.git("checkout", "-q", "-b", "feature/SHOP-412")
    git_repo.commit("wip SHOP-412", {"src/Price.php": "a\nb\n", "src/Old.php": "o\n"})
    git_repo.git("checkout", "-q", "main")
    git_repo.commit("feat: round prices SHOP-412", {"src/Price.php": "a\nb\n"})
    arguments = ["export-case", "SHOP-412", "--source", "worklog", "--actual-total", "2"]
    arguments += ["--repo", str(git_repo.path)]

    assert cli.main([*arguments, "--rev", "main"]) == 0
    assert read(home / "eval-cases" / "SHOP-412.json")["diff"]["files"] == ["src/Price.php"]
    assert cli.main(arguments) == 0
    assert read(home / "eval-cases" / "SHOP-412.json")["diff"]["files"] == [
        "src/Old.php",
        "src/Price.php",
    ]


def test_escaped_then_metrics(home, git_repo, capsys):
    sha = git_repo.commit("fix: round prices - SHOP-412", {"src/Price.php": "x"})
    assert cli.main(["escaped", "--commit", sha, "--repo", str(git_repo.path)]) == 0
    assert cli.main(["review-finding", "SHOP-412", "F1", "--disposition", "accepted"]) == 0
    capsys.readouterr()

    assert cli.main(["metrics", "--repo", str(git_repo.path)]) == 0
    text = capsys.readouterr().out
    assert "| SHOP-412 | git | 1 | 1 | 1 | 0 | 100% | 100% |" in text
    escaped = "| pass | 2 | 1 | 0 | 0 | 1 | 100% | 1 | reviewer passed, defect escaped |"
    assert f"| SHOP-412 {escaped}" in text

    assert cli.main(["metrics", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["reviewer"]["escaped_after_review"] == 1
    assert data["analyst"]["without_changed_files"] == ["SHOP-412"]


def test_incident_case_errors(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("pack: [unclosed")
    with pytest.raises(ValueError, match="Cannot read the case"):
        incident_case(bad)
    bad.write_text("id: x\n")
    with pytest.raises(ValueError, match="not an incident-scout case"):
        incident_case(bad)
    fallback = tmp_path / "incident-3-disk.yaml"
    fallback.write_text(yaml.safe_dump({"pack": {"summary": "disk 95%\nfull"}}))
    assert incident_case(fallback) == IncidentCase("incident-3-disk", "disk 95% full")


def test_append_escaped_and_report_blocks_on_malformed_files(tmp_path):
    (tmp_path / "escaped.json").write_text("{}")
    with pytest.raises(ValueError, match="JSON list"):
        append_escaped(tmp_path, {"commit": "a", "ref": None})
    (tmp_path / "escaped.json").write_text("[1")
    with pytest.raises(ValueError, match="not valid JSON"):
        append_escaped(tmp_path, {"commit": "a", "ref": None})
    (tmp_path / "escaped.json").write_text('["junk"]')
    assert append_escaped(tmp_path, {"commit": "a", "ref": None})

    (tmp_path / "review.json").write_text("[]")
    with pytest.raises(ValueError, match="JSON object"):
        set_disposition(tmp_path, ["F1"], "accepted")

    (tmp_path / "reviewer-qa.01.md").write_text(
        "```json harness-review\n{broken\n```\n\n```json harness-review\n[1]\n```\n"
        "```json harness-review\n" + json.dumps(REVIEW) + "\n```\n"
    )
    assert report_blocks(tmp_path, "review") == [REVIEW]
    assert report_blocks(tmp_path, "implementation") == []


REPORTS = {
    "plain": "Done.\n\n```json harness-implementation\n{body}\n```",
    "indented in a list": "- Files:\n\n  ```json harness-implementation\n  {body}\n  ```",
    "tab and trailing blanks": "```json\tharness-implementation \n{body}\n```  ",
    "reserved kind": "```json harness-outcome\n{body}\n```",
    "nested too deep": "```json harness-implementation\n" + "[" * 10**5 + "]" * 10**5 + "\n```",
}


@pytest.mark.parametrize("report", REPORTS.values(), ids=REPORTS.keys())
def test_report_blocks_reads_the_blocks_save_artifacts_saves(report, tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(HOOKS))
    save_artifacts = importlib.import_module("save_artifacts")
    text = report.format(body=json.dumps({"ticket": "SHOP-412", "files": ["src/Price.php"]}))
    (tmp_path / "implementer.01.md").write_text(text + "\n")

    for kind in ("implementation", "outcome"):
        blocks = save_artifacts.structured_blocks(text)
        saved = [block.value for block in blocks if block.kind == kind and block.value]
        assert report_blocks(tmp_path, kind) == saved
