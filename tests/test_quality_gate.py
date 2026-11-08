import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from tests.test_artifacts import fenced, said, transcript, user
from tests.test_hooks import HOOKS, git_repository, hook, write_settings

REPORTER = """\
import json, os, sys
label, code, *files = sys.argv[1:]
print(label, os.getcwd(), json.dumps(files))
sys.exit(int(code))
"""


@pytest.fixture
def check(tmp_path: Path):
    script = tmp_path / "check.py"
    script.write_text(REPORTER)

    def command(label: str, code: int = 0, files: bool = True) -> str:
        return f"{sys.executable} {script} {label} {code}" + (" {files}" if files else "")

    return command


def configure(repo: Path, gate: Any) -> None:
    (repo / ".claude").mkdir(exist_ok=True)
    (repo / ".claude" / "agent-harness.json").write_text(json.dumps({"gate": gate}))


def touch(repo: Path, *names: str) -> None:
    for name in names:
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text("x = 1\n")


def finished(
    tmp_path: Path,
    cwd: Path,
    report: str = "Done.",
    agent: str | None = "agent-harness:implementer",
    **fields: Any,
) -> dict[str, Any]:
    path = transcript(tmp_path, user("Implement SHOP-412"), said(report), name="agent-i1")
    event: dict[str, Any] = {
        "hook_event_name": "SubagentStop",
        "agent_id": "i1",
        "agent_transcript_path": str(path),
        "cwd": str(cwd),
        "stop_hook_active": False,
        **fields,
    }
    if agent:
        event["agent_type"] = agent
    return event


def gate(
    tmp_path: Path, event: dict[str, Any], project: Path | None = None, **env: str
) -> dict[str, Any] | None:
    settings = {
        "HARNESS_HOME": str(tmp_path / "home"),
        "HARNESS_TICKET": "",
        "HARNESS_GATE_COMMANDS": "",
        "CLAUDE_PROJECT_DIR": str(project or event["cwd"]),
        **env,
    }
    result = hook("quality_gate.py", event, **settings)
    assert result.returncode == 0
    assert result.stderr == ""
    return json.loads(result.stdout) if result.stdout else None


def gates(tmp_path: Path) -> Any:
    return json.loads((tmp_path / "home/tickets/SHOP-412/gates.json").read_text())


def test_a_passing_gate_lets_the_implementer_finish(tmp_path, check):
    repo = git_repository(tmp_path / "shop")
    configure(repo, {"commands": [{"cmd": check("lint"), "match": r"\.py$"}]})
    touch(repo, "src/shop.py", "notes.txt")

    assert gate(tmp_path, finished(tmp_path, repo)) is None

    result = gates(tmp_path)
    assert result["ticket"] == "SHOP-412"
    assert result["repo"] == str(repo)
    assert result["passed"] is True
    assert result["blocks"] == 0
    [command] = result["commands"]
    assert command["exit"] == 0
    assert command["repo"] == str(repo)
    assert command["output_tail"] == f'lint {repo} ["src/shop.py"]'
    assert isinstance(command["seconds"], float)
    assert command["cmd"].endswith("lint 0 src/shop.py")


def test_a_failing_gate_blocks_with_the_tail_of_its_output(tmp_path):
    repo = git_repository(tmp_path / "shop")
    noisy = "import sys; print('\\n'.join(f'line {i}' for i in range(100))); sys.exit(3)"
    configure(repo, {"commands": [{"cmd": f'{sys.executable} -c "{noisy}"'}]})
    touch(repo, "src/shop.py")

    answer = gate(tmp_path, finished(tmp_path, repo))

    assert answer is not None
    assert answer["decision"] == "block"
    assert "block 1 of at most 2" in answer["reason"]
    assert f"(in {repo}, exit 3)" in answer["reason"]
    assert "line 99" in answer["reason"]
    assert "line 59" not in answer["reason"]
    result = gates(tmp_path)
    assert result["passed"] is False
    assert result["blocks"] == 1
    [command] = result["commands"]
    assert command["exit"] == 3
    assert command["output_tail"].splitlines() == [f"line {i}" for i in range(60, 100)]


def test_files_are_quoted_and_filtered_by_match(tmp_path, check):
    repo = git_repository(tmp_path / "shop")
    commands = [
        {"cmd": check("python"), "match": r"\.py$"},
        {"cmd": check("php"), "match": r"\.php$"},
        {"cmd": check("everything", files=False)},
        {"cmd": check("server"), "match": r"^src/server/"},
    ]
    configure(repo, {"commands": commands})
    touch(repo, "src/my shop.py", "src/server/feed.py", "README.md")

    assert gate(tmp_path, finished(tmp_path, repo)) is None

    tails = [command["output_tail"] for command in gates(tmp_path)["commands"]]
    assert tails == [
        f'python {repo} ["src/my shop.py", "src/server/feed.py"]',
        f"everything {repo} []",
        f'server {repo} ["src/server/feed.py"]',
    ]


def test_the_implementation_block_names_the_files(tmp_path, check):
    repo = git_repository(tmp_path / "shop")
    platform = git_repository(tmp_path / "platform")
    shop = [{"cmd": check("shop"), "match": r"\.py$"}]
    configure(repo, {"commands": shop, "repositories": ["../platform"]})
    configure(platform, [{"cmd": check("platform")}])
    touch(repo, "src/a.py", "src/b.py", "src/server/c.py")
    touch(platform, "lib/feed.py")
    block = {
        "ticket": "SHOP-412",
        "files": ["src/a.py", "c.py", str(platform / "lib/feed.py"), "src/gone.py", 7],
        "checks": [],
    }
    report = "Done.\n\n" + fenced("implementation", block)

    assert gate(tmp_path, finished(tmp_path, repo / "src" / "server", report)) is None

    result = gates(tmp_path)
    assert result["repo"] == str(repo)
    assert [command["output_tail"] for command in result["commands"]] == [
        f'shop {repo} ["src/a.py", "src/server/c.py"]',
        f'platform {platform} ["lib/feed.py"]',
    ]


def test_a_renamed_file_is_checked_under_its_new_name(tmp_path, check):
    repo = git_repository(tmp_path / "shop")
    touch(repo, "src/old.py")
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, "add", "-A"], check=True)  # noqa: S603
    subprocess.run([*git, "commit", "-qm", "init"], check=True)  # noqa: S603
    subprocess.run([*git, "mv", "src/old.py", "src/new.py"], check=True)  # noqa: S603
    configure(repo, [{"cmd": check("lint"), "match": r"\.py$"}])

    assert gate(tmp_path, finished(tmp_path, repo)) is None
    assert gates(tmp_path)["commands"][0]["output_tail"] == f'lint {repo} ["src/new.py"]'


def test_without_a_configuration_the_gate_does_nothing(tmp_path):
    repo = git_repository(tmp_path / "shop")
    touch(repo, "src/shop.py")

    assert gate(tmp_path, finished(tmp_path, repo)) is None
    assert not (tmp_path / "home").exists()


@pytest.mark.parametrize("agent", [None, "agent-harness:reviewer-qa", "Explore"])
def test_the_gate_runs_only_for_the_implementer(tmp_path, check, agent):
    repo = git_repository(tmp_path / "shop")
    configure(repo, [{"cmd": check("lint", 1)}])
    touch(repo, "src/shop.py")

    assert gate(tmp_path, finished(tmp_path, repo, agent=agent)) is None
    assert not (tmp_path / "home").exists()


def test_a_configured_gate_with_nothing_changed_records_a_pass(tmp_path, check):
    repo = git_repository(tmp_path / "shop")
    configure(repo, [{"cmd": check("lint", 1)}])
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)  # noqa: S603, S607
    commit = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", "commit"]
    subprocess.run([*commit, "-qm", "init"], check=True)  # noqa: S603

    assert gate(tmp_path, finished(tmp_path, repo)) is None
    assert gates(tmp_path)["passed"] is True
    assert gates(tmp_path)["commands"] == []


@pytest.mark.parametrize("shape", ["list", "object"])
def test_env_commands_win_over_the_file_in_the_session_repository(tmp_path, check, shape):
    repo = git_repository(tmp_path / "shop")
    configure(repo, [{"cmd": check("file", 1)}])
    touch(repo, "src/shop.py")
    commands = [{"cmd": check("env"), "match": r"\.py$"}]
    env = commands if shape == "list" else {"commands": commands, "timeout": 60}

    answer = gate(tmp_path, finished(tmp_path, repo), HARNESS_GATE_COMMANDS=json.dumps(env))

    assert answer is None
    assert gates(tmp_path)["commands"][0]["output_tail"] == f'env {repo} ["src/shop.py"]'


@pytest.mark.parametrize("env", ["{not json", "[]", '{"commands": "lint"}', '[{"cmd": ""}]'])
def test_unusable_env_commands_fall_back_to_the_file(tmp_path, check, env):
    repo = git_repository(tmp_path / "shop")
    configure(repo, [{"cmd": check("file")}])
    touch(repo, "src/shop.py")

    assert gate(tmp_path, finished(tmp_path, repo), HARNESS_GATE_COMMANDS=env) is None
    assert gates(tmp_path)["commands"][0]["output_tail"].startswith("file ")


def test_another_repository_uses_its_own_settings_not_the_session_env(tmp_path, check):
    project = git_repository(tmp_path / "shop")
    platform = git_repository(tmp_path / "platform")
    touch(platform, "lib/feed.py")
    write_settings(
        platform, "settings.local.json", HARNESS_GATE_COMMANDS=json.dumps([{"cmd": check("own")}])
    )
    report = fenced("implementation", {"files": [str(platform / "lib/feed.py")]})
    session_env = json.dumps(
        {"commands": [{"cmd": check("session", 1)}], "repositories": [str(platform)]}
    )

    answer = gate(tmp_path, finished(tmp_path, project, report), HARNESS_GATE_COMMANDS=session_env)

    assert answer is None
    [command] = gates(tmp_path)["commands"]
    assert command["output_tail"] == f'own {platform} ["lib/feed.py"]'


def test_the_gate_blocks_at_most_twice_in_a_row(tmp_path, check):
    repo = git_repository(tmp_path / "shop")
    configure(repo, [{"cmd": check("lint", 1)}])
    touch(repo, "src/shop.py")

    answers = [
        gate(tmp_path, finished(tmp_path, repo, stop_hook_active=active))
        for active in (False, True, True)
    ]

    first, second, third = (answer or {} for answer in answers)
    assert [first.get("decision"), second.get("decision"), third.get("decision")] == [
        "block",
        "block",
        None,
    ]
    assert "block 2 of at most 2" in second["reason"]
    assert third["systemMessage"].startswith("The quality gate still fails")
    assert gates(tmp_path)["passed"] is False
    assert gates(tmp_path)["blocks"] == 2

    resumed = gate(tmp_path, finished(tmp_path, repo, stop_hook_active=False))
    assert resumed is not None
    assert resumed["decision"] == "block"
    assert gates(tmp_path)["blocks"] == 1


def test_a_resumed_implementer_gets_a_fresh_count(tmp_path, check):
    repo = git_repository(tmp_path / "shop")
    configure(repo, [{"cmd": check("lint", 1)}])
    touch(repo, "src/shop.py")

    for _ in range(2):
        answer = gate(tmp_path, finished(tmp_path, repo, stop_hook_active=False))
        assert answer is not None
        assert "block 1 of at most 2" in answer["reason"]


def test_a_pass_after_a_block_records_the_block_and_starts_over(tmp_path, check):
    repo = git_repository(tmp_path / "shop")
    failing = [{"cmd": check("lint", 1)}]
    configure(repo, failing)
    touch(repo, "src/shop.py")
    assert gate(tmp_path, finished(tmp_path, repo)) is not None

    configure(repo, [{"cmd": check("lint")}])
    assert gate(tmp_path, finished(tmp_path, repo, stop_hook_active=True)) is None
    assert (gates(tmp_path)["passed"], gates(tmp_path)["blocks"]) == (True, 1)

    configure(repo, failing)
    answer = gate(tmp_path, finished(tmp_path, repo, stop_hook_active=True))
    assert answer is not None
    assert "block 1 of at most 2" in answer["reason"]


def handing_back(
    tmp_path: Path,
    cwd: Path,
    report: str = "Done.",
    agent: str = "agent-harness:implementer",
    tool: str = "SubagentHandback",
) -> dict[str, Any]:
    transcript(tmp_path, user("Implement SHOP-412"), name="agent-i1")
    return {
        "hook_event_name": "PreToolUse",
        "agent_id": "i1",
        "agent_type": agent,
        "transcript_path": f"{tmp_path}.jsonl",
        "cwd": str(cwd),
        "tool_name": tool,
        "tool_input": {"message": report},
    }


def denied(answer: dict[str, Any] | None) -> str | None:
    if answer is None or "hookSpecificOutput" not in answer:
        return None
    output = answer["hookSpecificOutput"]
    assert (output["hookEventName"], output["permissionDecision"]) == ("PreToolUse", "deny")
    return str(output["permissionDecisionReason"])


def test_a_failing_handback_is_denied_with_the_gate_output(tmp_path, check):
    repo = git_repository(tmp_path / "shop")
    configure(repo, [{"cmd": check("lint", 1), "match": r"\.py$"}])
    touch(repo, "src/shop.py")

    reason = denied(gate(tmp_path, handing_back(tmp_path, repo)))

    assert reason is not None
    assert "block 1 of at most 2" in reason
    assert "call SubagentHandback again with the whole report" in reason
    assert f'lint {repo} ["src/shop.py"]' in reason
    assert (gates(tmp_path)["passed"], gates(tmp_path)["blocks"]) == (False, 1)


def test_the_handback_checks_the_files_in_its_message(tmp_path, check):
    repo = git_repository(tmp_path / "shop")
    configure(repo, [{"cmd": check("lint")}])
    touch(repo, "src/a.py", "src/b.py")
    report = "Done.\n\n" + fenced("implementation", {"files": ["src/b.py"]})

    assert gate(tmp_path, handing_back(tmp_path, repo, report)) is None

    assert gates(tmp_path)["commands"][0]["output_tail"] == f'lint {repo} ["src/b.py"]'


@pytest.mark.parametrize(
    ("agent", "tool"),
    [("agent-harness:reviewer-qa", "SubagentHandback"), ("agent-harness:implementer", "Bash")],
)
def test_the_handback_path_is_only_the_implementers_handback(tmp_path, check, agent, tool):
    repo = git_repository(tmp_path / "shop")
    configure(repo, [{"cmd": check("lint", 1)}])
    touch(repo, "src/shop.py")

    assert gate(tmp_path, handing_back(tmp_path, repo, agent=agent, tool=tool)) is None
    assert not (tmp_path / "home/tickets").exists()


def test_a_handback_that_passed_is_not_checked_again_when_the_run_stops(tmp_path, check):
    repo = git_repository(tmp_path / "shop")
    configure(repo, [{"cmd": check("lint")}])
    touch(repo, "src/shop.py")
    assert gate(tmp_path, handing_back(tmp_path, repo)) is None

    configure(repo, [{"cmd": check("lint", 1)}])
    assert gate(tmp_path, finished(tmp_path, repo)) is None
    assert gates(tmp_path)["passed"] is True

    answer = gate(tmp_path, finished(tmp_path, repo))
    assert answer is not None
    assert "block 1 of at most 2" in answer["reason"]


def test_handback_and_stop_share_one_block_count(tmp_path, check):
    repo = git_repository(tmp_path / "shop")
    configure(repo, [{"cmd": check("lint", 1)}])
    touch(repo, "src/shop.py")

    assert "block 1 of at most 2" in (denied(gate(tmp_path, handing_back(tmp_path, repo))) or "")
    stopped = gate(tmp_path, finished(tmp_path, repo, stop_hook_active=False))
    assert stopped is not None
    assert "block 2 of at most 2" in stopped["reason"]
    final = gate(tmp_path, finished(tmp_path, repo, stop_hook_active=True))
    assert final is not None
    assert final["systemMessage"].startswith("The quality gate still fails")
    assert gates(tmp_path)["blocks"] == 2


def test_a_handback_let_through_after_the_limit_is_not_blocked_at_stop(tmp_path, check):
    repo = git_repository(tmp_path / "shop")
    configure(repo, [{"cmd": check("lint", 1)}])
    touch(repo, "src/shop.py")

    answers = [gate(tmp_path, handing_back(tmp_path, repo)) for _ in range(3)]

    assert [denied(answer) is not None for answer in answers] == [True, True, False]
    assert answers[2] == {
        "systemMessage": "The quality gate still fails and the implementer finished anyway: "
        f"{gates(tmp_path)['commands'][0]['cmd']}. "
        f"See {tmp_path / 'home/tickets/SHOP-412/gates.json'}."
    }
    assert gate(tmp_path, finished(tmp_path, repo, stop_hook_active=False)) is None
    assert gates(tmp_path)["blocks"] == 2


def test_the_handback_path_denies_at_most_twice(tmp_path, check):
    repo = git_repository(tmp_path / "shop")
    configure(repo, {"commands": [{"cmd": check("lint", 1)}], "max_blocks": 5})
    touch(repo, "src/shop.py")

    answers = [gate(tmp_path, handing_back(tmp_path, repo)) for _ in range(3)]

    assert [denied(answer) is not None for answer in answers] == [True, True, False]
    assert "block 2 of at most 2" in (denied(answers[1]) or "")


def test_a_count_written_by_an_older_version_still_counts(tmp_path, check):
    repo = git_repository(tmp_path / "shop")
    configure(repo, [{"cmd": check("lint", 1)}])
    touch(repo, "src/shop.py")
    state = tmp_path / "home/tickets/SHOP-412/gate-blocks.json"
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps({"i1": 1, "other": {"blocks": "x"}, "third": True}))

    answer = gate(tmp_path, finished(tmp_path, repo, stop_hook_active=True))

    assert answer is not None
    assert "block 2 of at most 2" in answer["reason"]
    assert json.loads(state.read_text()) == {"i1": {"blocks": 2, "handback": None}}


@pytest.mark.parametrize(("max_blocks", "blocked"), [(0, False), (1, True), ("3", True)])
def test_max_blocks_is_configurable(tmp_path, check, max_blocks, blocked):
    repo = git_repository(tmp_path / "shop")
    configure(repo, {"commands": [{"cmd": check("lint", 1)}], "max_blocks": max_blocks})
    touch(repo, "src/shop.py")

    answer = gate(tmp_path, finished(tmp_path, repo))

    assert answer is not None
    assert ("decision" in answer) is blocked


def test_a_slow_check_times_out(tmp_path):
    repo = git_repository(tmp_path / "shop")
    configure(repo, {"commands": [{"cmd": "sleep 30; echo late"}], "timeout": 0.5})
    touch(repo, "src/shop.py")
    started = time.monotonic()

    answer = gate(tmp_path, finished(tmp_path, repo))

    assert time.monotonic() - started < 4
    assert answer is not None
    assert "(in " in answer["reason"]
    assert "did not finish" in answer["reason"]
    [command] = gates(tmp_path)["commands"]
    assert command["exit"] is None
    assert command["output_tail"].endswith("Did not finish within 0.5 s.")


@pytest.mark.parametrize("timeout", ["abc", "nan", "inf", -1, 0, True, None, 1e400, 10**400])
def test_an_invalid_timeout_falls_back_to_the_default(tmp_path, check, timeout):
    repo = git_repository(tmp_path / "shop")
    configure(repo, {"commands": [{"cmd": check("lint")}], "timeout": timeout})
    touch(repo, "src/shop.py")

    assert gate(tmp_path, finished(tmp_path, repo)) is None
    assert gates(tmp_path)["commands"][0]["exit"] == 0


def test_unusable_commands_are_skipped(tmp_path, check):
    repo = git_repository(tmp_path / "shop")
    commands = [
        "lint",
        {"cmd": check("bad regex", 1), "match": "("},
        {"cmd": check("bad match", 1), "match": 7},
        {"cmd": 7},
        {"cmd": check("lint")},
    ]
    configure(repo, {"commands": commands})
    touch(repo, "src/shop.py")

    assert gate(tmp_path, finished(tmp_path, repo)) is None
    assert [c["output_tail"].split()[0] for c in gates(tmp_path)["commands"]] == ["lint"]


@pytest.mark.parametrize("stdin", ["not json", "[]", "{}"])
def test_bad_input_never_fails(tmp_path, stdin):
    result = subprocess.run(  # noqa: S603
        [sys.executable, str(HOOKS / "quality_gate.py")],
        input=stdin,
        capture_output=True,
        text=True,
        env={**os.environ, "HARNESS_HOME": str(tmp_path / "home")},
        check=False,
    )
    assert result.returncode == 0
    assert result.stdout == ""


def test_the_example_configuration_is_usable():
    gate = json.loads((HOOKS.parent / "examples" / "agent-harness.json").read_text())["gate"]
    for command in gate["commands"]:
        assert re.compile(command["match"])
        assert command["cmd"].strip()
    assert gate["timeout"] > 0


def test_another_hook_continuing_the_run_does_not_reopen_the_count(tmp_path, check):
    repo = git_repository(tmp_path / "shop")
    configure(repo, [{"cmd": check("lint", 1)}])
    touch(repo, "src/shop.py")

    answers = [
        gate(tmp_path, finished(tmp_path, repo, stop_hook_active=active))
        for active in (False, True, True, True)
    ]

    assert [(answer or {}).get("decision") for answer in answers] == ["block", "block", None, None]
    assert (answers[3] or {})["systemMessage"].startswith("The quality gate still fails")
    assert gates(tmp_path)["blocks"] == 2


def test_a_background_child_holding_the_output_does_not_time_the_check_out(tmp_path):
    repo = git_repository(tmp_path / "shop")
    configure(repo, {"commands": [{"cmd": "(sleep 8 &); echo ok"}], "timeout": 3})
    touch(repo, "src/shop.py")
    started = time.monotonic()

    assert gate(tmp_path, finished(tmp_path, repo)) is None

    assert time.monotonic() - started < 3
    [command] = gates(tmp_path)["commands"]
    assert (command["exit"], command["output_tail"]) == (0, "ok")


def test_an_unwritable_home_skips_the_gate_and_says_so(tmp_path):
    repo = git_repository(tmp_path / "shop")
    marker = tmp_path / "ran"
    configure(repo, [{"cmd": f"touch {marker}; exit 1"}])
    touch(repo, "src/shop.py")
    (tmp_path / "home").write_text("a file, not a directory")

    for event in (finished(tmp_path, repo), handing_back(tmp_path, repo)):
        answer = gate(tmp_path, event)
        assert answer is not None
        assert set(answer) == {"systemMessage"}
        assert "did not run the quality gate" in answer["systemMessage"]
    assert not marker.exists()


def test_a_repository_outside_the_session_is_not_trusted(tmp_path, check):
    repo = git_repository(tmp_path / "shop")
    other = git_repository(tmp_path / "other")
    marker = tmp_path / "ran"
    configure(repo, [{"cmd": check("shop")}])
    configure(other, [{"cmd": f"touch {marker}"}])
    write_settings(
        other, "settings.local.json", HARNESS_GATE_COMMANDS=f'[{{"cmd": "touch {marker}"}}]'
    )
    touch(repo, "src/a.py")
    touch(other, "lib/b.py")
    report = fenced("implementation", {"files": ["src/a.py", str(other / "lib/b.py")]})

    answer = gate(tmp_path, finished(tmp_path, repo, report))

    assert not marker.exists()
    assert answer is not None
    assert "did not check lib/b.py in " in answer["systemMessage"]
    assert "gate.repositories" in answer["systemMessage"]
    result = gates(tmp_path)
    assert [command["output_tail"] for command in result["commands"]] == [
        f'shop {repo} ["src/a.py"]'
    ]
    assert result["skipped"] == [{"repo": str(other), "files": ["lib/b.py"]}]


def test_an_untrusted_repository_alone_runs_nothing(tmp_path):
    repo = git_repository(tmp_path / "shop")
    other = git_repository(tmp_path / "other")
    marker = tmp_path / "ran"
    configure(other, [{"cmd": f"touch {marker}"}])
    touch(other, "lib/b.py")
    report = fenced("implementation", {"files": [str(other / "lib/b.py")]})

    assert gate(tmp_path, finished(tmp_path, other, report), project=repo) is None
    assert not marker.exists()
    assert not (tmp_path / "home").exists()


def retro(tmp_path: Path, event: dict[str, Any]) -> dict[str, Any]:
    path = transcript(tmp_path, user("retro for SHOP-412. Do not write yet."), name="agent-i1")
    return {**event, "agent_transcript_path": str(path)}


def test_a_retro_run_is_not_gated(tmp_path, check):
    repo = git_repository(tmp_path / "shop")
    configure(repo, [{"cmd": check("lint", 1)}])
    touch(repo, "src/shop.py")

    assert gate(tmp_path, retro(tmp_path, finished(tmp_path, repo))) is None
    assert gate(tmp_path, retro(tmp_path, handing_back(tmp_path, repo))) is None
    assert not (tmp_path / "home").exists()


@pytest.mark.parametrize(
    ("prompt", "block", "key"),
    [
        ("Fix the UTF-8 export for SHOP-412", {}, "SHOP-412"),
        ("Follow PSR-12 and SHA-256 in SHOP-412", {}, "SHOP-412"),
        ("Implement SHOP-412", {"ticket": "SHOP-77"}, "SHOP-77"),
        ("Implement SHOP-412", {"ticket": "ISO-8601"}, "SHOP-412"),
        ("Implement SHOP-412", {"ticket": "not a key"}, "SHOP-412"),
    ],
)
def test_the_ticket_skips_standards_and_prefers_the_block(tmp_path, check, prompt, block, key):
    repo = git_repository(tmp_path / "shop")
    configure(repo, [{"cmd": check("lint")}])
    touch(repo, "src/shop.py")
    report = "Done.\n\n" + fenced("implementation", {**block, "files": ["src/shop.py"]})
    event = finished(tmp_path, repo, report)
    event["agent_transcript_path"] = str(
        transcript(tmp_path, user(prompt), said(report), name="agent-i1")
    )

    assert gate(tmp_path, event) is None
    assert [path.name for path in (tmp_path / "home/tickets").iterdir()] == [key]
