import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

WORKFLOWS = sorted((Path(__file__).resolve().parents[1] / "workflows").glob("*.js"))


def meta_block(script: str) -> str:
    match = re.match(r"export const meta = (\{.*?\n\})\n", script, re.S)
    assert match, "a workflow starts with `export const meta = {...}`"
    return match.group(1)


def test_there_are_workflows():
    assert WORKFLOWS


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_meta_is_a_pure_literal(path):
    meta = meta_block(path.read_text())
    for dynamic in ("`", "${", "..."):
        assert dynamic not in meta
    assert re.search(r"\bname: '[a-z0-9-]+'", meta)
    assert re.search(r"\bdescription: '", meta)


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_every_phase_an_agent_uses_is_declared(path):
    script = path.read_text()
    declared = set(re.findall(r"\{ title: '([^']+)'", meta_block(script)))
    used = set(re.findall(r"phase: '([^']+)'", script))
    used |= set(re.findall(r"phase\('([^']+)'\)", script))
    assert used <= declared


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_no_clock_or_randomness(path):
    body = path.read_text()
    for call in ("Date.now(", "Math.random(", "new Date()"):
        assert call not in body


ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")


def run_workflow(name: str, scenario: dict[str, Any]) -> dict[str, Any]:
    result = subprocess.run(  # noqa: S603
        [
            NODE or "node",
            str(ROOT / "tests/js/run_workflow.mjs"),
            str(ROOT / "workflows" / name),
            json.dumps(scenario),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    out: dict[str, Any] = json.loads(result.stdout)
    return out


def finding(change: str, risk: str) -> dict[str, Any]:
    return {
        "component": "http-foundation",
        "version": "1.0.1",
        "change": change,
        "usage": ["shop:src/a.php:1"],
        "risk": risk,
        "why": "w",
        "test": "t",
    }


UPGRADE = {
    "dependency": "Framework 1.0",
    "from": "1.0.0",
    "to": "1.0.9",
    "changelog": "/work/CHANGELOG.md",
    "repos": [{"name": "shop", "path": "/repo/shop", "ref": "origin/main", "paths": ["src"]}],
    "groups": [{"name": "http", "components": ["http-foundation"]}],
    "versions": {"http-foundation": "1.0.4"},
}


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_upgrade_impact_verifies_by_risk_and_sorts_out_refuted_risks():
    scenario = {
        "args": UPGRADE,
        "find": {
            "reviewed": 7,
            "findings": [
                finding("medium kept", "medium"),
                finding("high kept", "high"),
                finding("medium refuted", "medium"),
                finding("high disputed", "high"),
                finding("benign", "low"),
            ],
        },
        "refute": ["medium refuted", ["Lens: our usage", '"change":"high disputed"']],
    }

    out = run_workflow("upgrade-impact.js", scenario)
    result = out["result"]

    verifies = [c for c in out["calls"] if c.startswith("verify:")]
    assert out["calls"].count("find:http") == 1
    assert len(verifies) == 2 + 2 + 1 + 1
    assert [f["change"] for f in result["confirmed"]] == [
        "high kept",
        "high disputed",
        "medium kept",
    ]
    assert [f["disputed"] for f in result["confirmed"]] == [False, True, False]
    assert [r["change"] for r in result["refuted"]] == ["medium refuted"]
    assert [f["change"] for f in result["low"]] == ["benign"]
    assert result["reviewed"] == 7
    assert result["failedGroups"] == []


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_upgrade_impact_tells_finders_the_installed_version():
    scenario = {"args": UPGRADE, "find": {"reviewed": 0, "findings": []}, "refute": []}
    out = run_workflow("upgrade-impact.js", scenario)
    assert out["calls"] == ["find:http"]
    assert out["logs"] == ["0 entries reviewed, 0 risks kept, 0 refuted, 0 low"]


def test_upgrade_impact_rejects_missing_args():
    if NODE is None:
        pytest.skip("node is not installed")
    with pytest.raises(subprocess.CalledProcessError):
        run_workflow("upgrade-impact.js", {"args": {}, "find": {}, "refute": []})
