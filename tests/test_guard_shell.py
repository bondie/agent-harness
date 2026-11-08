import importlib.util
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "hooks" / "guard_shell.py"
SPEC = importlib.util.spec_from_file_location("guard_shell", SCRIPT)
assert SPEC is not None
assert SPEC.loader is not None
guard_shell = importlib.util.module_from_spec(SPEC)
sys.modules["guard_shell"] = guard_shell
SPEC.loader.exec_module(guard_shell)

ENV = {"TMPDIR": "/sandbox/tmp", "HOME": "/work/home", "HARNESS_HOME": "/work/no-harness-home"}
SUBAGENT = "agent-harness:implementer"
SCRATCH = "/tmp/claude-501/session/scratchpad"  # noqa: S108


def decide(
    command: str, agent: str | None = SUBAGENT, cwd: str = "/work/shop", **env: str
) -> str | None:
    event: dict[str, Any] = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "cwd": cwd,
    }
    if agent:
        event["agent_type"] = agent
    verdict = guard_shell.decision(event, {**ENV, **env})
    if verdict is None:
        return None
    output = verdict["hookSpecificOutput"]
    assert output["hookEventName"] == "PreToolUse"
    assert output["permissionDecision"] == "deny"
    reason: str = output["permissionDecisionReason"]
    return reason


def rule(command: str, **kwargs: Any) -> str | None:
    reason = decide(command, **kwargs)
    if reason is None:
        return None
    match = re.match(r"agent-harness guard, rule ([\w-]+): ", reason)
    assert match, reason
    return match.group(1)


@pytest.mark.parametrize(
    "command",
    [
        "git push --force",
        "git push -f origin main",
        "git push -uf origin main",
        "git push origin +main",
        "git push origin +HEAD:main",
        "/usr/bin/git push --force",
        "git -C /work/other push --force",
        "ls && git push --force",
        "bash -c 'git push --force'",
        "git push --force --force-with-lease",
    ],
)
def test_force_push_is_denied(command):
    assert rule(command) == "force-push"


@pytest.mark.parametrize(
    "command",
    [
        "git push",
        "git push origin main",
        "git push -u origin feature",
        "git push -o ci.skip origin main",
        "git push --follow-tags",
        "git fetch --force",
    ],
)
def test_a_normal_push_is_allowed(command):
    assert rule(command) is None


def test_force_with_lease_is_denied_to_subagents_by_default():
    assert rule("git push --force-with-lease") == "force-with-lease"
    assert rule("git push --force-with-lease=main:abc123 origin main") == "force-with-lease"


def test_force_with_lease_is_allowed_to_the_main_agent_by_default(tmp_path):
    home = guard_config(tmp_path, {"scope": "all"})
    assert rule("git push --force-with-lease", agent=None, HARNESS_HOME=home) is None
    assert rule("git push --force", agent=None, HARNESS_HOME=home) == "force-push"


@pytest.mark.parametrize(
    ("setting", "agent", "denied"),
    [(True, SUBAGENT, False), (False, None, True), (False, SUBAGENT, True), ("main", None, False)],
)
def test_force_with_lease_follows_the_setting(tmp_path, setting, agent, denied):
    home = guard_config(tmp_path, {"scope": "all", "allow_force_with_lease": setting})
    result = rule("git push --force-with-lease", agent=agent, HARNESS_HOME=home)
    assert result == ("force-with-lease" if denied else None)


@pytest.mark.parametrize(
    "command",
    [
        "git reset --hard",
        "git reset --hard HEAD~1",
        "git clean -fd",
        "git clean -xdf",
        "git clean --force",
        "git checkout -- .",
        "git checkout .",
        "git checkout HEAD -- .",
        "git restore .",
        "git restore :/",
        "git restore --staged --worktree .",
        "git restore -SW .",
        "cd src && git checkout -- ..",
        "git checkout /work/shop",
        "git -C /work/shop reset --hard",
        "cd /tmp/claude-501/x; git reset --hard",
        "sh -c 'git clean -fdx'",
    ],
)
def test_discarding_work_is_denied(command):
    assert rule(command) == "git-discard"


@pytest.mark.parametrize(
    "command",
    [
        "git reset --soft HEAD~1",
        "git reset HEAD src/shop.py",
        "git clean -n",
        "git clean -nd -f",
        "git clean --dry-run --force",
        "git checkout -b feature",
        "git checkout main",
        "git checkout -- src/shop.py",
        "git restore src/shop.py",
        "git restore --staged .",
        f"git -C {SCRATCH}/repo reset --hard",
        f"cd {SCRATCH}/repo && git clean -fdx",
        f"git -C {SCRATCH}/repo checkout -- .",
    ],
)
def test_safe_git_commands_are_allowed(command):
    assert rule(command) is None


@pytest.mark.parametrize(
    "command",
    [
        "rm -rf build",
        "rm -r build",
        "rm -fr build",
        "rm -R build",
        "rm --recursive build",
        "rm build -rf",
        "rm -rf /",
        "rm -rf ~",
        "rm -rf ~/projects",
        "rm -rf ~other/projects",
        "rm -rf $HOME/projects",
        'rm -rf "$DIR"',
        "rm -rf $(pwd)/build",
        "rm -rf /tmp/claude-501",
        "rm -rf /tmp/claude-*",
        f"rm -rf {SCRATCH}/../../../../etc",
        "rm -rf /sandbox/tmp",
        f"rm -rf {SCRATCH}/x build",
        f"cd {SCRATCH}/x; rm -rf build",
        f"cd {SCRATCH}/x || rm -rf build",
        f"cd {SCRATCH}/x && ls || rm -rf build",
        f"(cd {SCRATCH}/x && ls); rm -rf build",
        "cd - && rm -rf build",
        "bash -c 'rm -rf build'",
        'sh -c "rm -rf build"',
        "bash -lc 'cd / && rm -rf usr'",
        "bash -c \"bash -c 'rm -rf build'\"",
        "zsh -c 'rm -rf build'",
        "sudo rm -rf /var/lib/app",
        "sudo -u root /bin/rm -rf /var/lib/app",
        "env FOO=1 rm -rf build",
        "FOO=1 BAR=2 rm -rf build",
        "\\rm -rf build",
        "'rm' -rf build",
        'r"m" -rf build',
        "$'\\x72m' -rf build",
        "nohup rm -rf build &",
        "timeout 5 rm -rf build",
        "nice -n 10 rm -rf build",
        "ls; rm -rf build",
        "ls && rm -rf build",
        "ls\nrm -rf build",
        "ls|rm -rf build",
        'echo "$(rm -rf build)"',
        "echo `rm -rf build`",
        "eval 'rm -rf build'",
        "bash <<'EOF'\nrm -rf build\nEOF",
        "bash -s <<< 'rm -rf build'",
        "if true; then rm -rf build; fi",
        "{ rm -rf build; }",
        "for d in a b; do rm -rf $d; done",
        "xargs rm -rf < dirs.txt",
        "find . -name '*.pyc' | xargs rm -rf",
        "find . -name node_modules | xargs -I{} rm -rf {}",
        "find . -delete",
        "find build -type f -delete",
        "find . -name x -exec rm -rf {} +",
        "find . -name '*.log' -exec /bin/rm {} \\;",
        "find -L . -name x -delete",
    ],
)
def test_recursive_delete_outside_the_allowed_dirs_is_denied(command):
    assert rule(command) == "rm-outside-allowed-dirs"


@pytest.mark.parametrize(
    "command",
    [
        f"rm -rf {SCRATCH}/x",
        "rm -rf /private/tmp/claude-501/x",
        "rm -rf /sandbox/tmp/x",
        "rm -rf $TMPDIR/x",
        "rm -rf ${TMPDIR}/x",
        f"rm -rf -- {SCRATCH}/x {SCRATCH}/y",
        f"cd {SCRATCH} && rm -rf build",
        f"cd {SCRATCH} && rm -rf ./build ../scratchpad/out",
        "rm file.txt",
        "rm -f file.txt",
        "rm -- -r",
        f"bash -c 'rm -rf {SCRATCH}/x'",
        f"sudo rm -rf {SCRATCH}/x",
        f"find {SCRATCH}/x -delete",
        f"find {SCRATCH}/x -type d | xargs rm -rf",
        "find . -name '*.pyc'",
        "find . -exec ls {} +",
        "echo 'rm -rf /'",
        "ls # rm -rf /",
        "git rm -r --cached build",
        "cat <<'EOF'\nrm -rf /\nEOF",
        "python3 - <<'EOF'\nimport shutil\nprint('rm -rf /')\nEOF",
    ],
)
def test_recursive_delete_inside_the_allowed_dirs_is_allowed(command):
    assert rule(command) is None


@pytest.mark.parametrize(
    "command",
    [
        "cat .env",
        "cat ./.env",
        "cat /work/shop/.env",
        "head .env.local",
        "tail -n 20 .env.production",
        "less .env",
        "more .env",
        "sed -n 1,5p .env",
        "sed 's/a/b/' .env",
        "awk '{print}' .env",
        "awk -F= '{print $2}' .env",
        "grep TOKEN .env",
        "grep -i -E 'token|key' .env",
        "grep -e TOKEN .env",
        "egrep TOKEN .env",
        "strings id_rsa",
        "xxd ~/.ssh/id_ed25519",
        "base64 server.key",
        "base64 -i cert.pem",
        "cat ~/.npmrc",
        "cat .pypirc",
        "cat ~/.netrc",
        "cat aws_credentials",
        "cat config/auth.json",
        "cat < .env",
        "cat .env | head",
        "cat .env | grep -v TOKEN",
        "bash -c 'cat .env'",
        "sudo cat /etc/app/.env",
        "/bin/cat .env",
        'cat "$HOME/.npmrc"',
        "cat README.md .env",
        "ls && cat .env",
    ],
)
def test_raw_reads_of_secret_files_are_denied(command):
    assert rule(command) == "secret-read"


@pytest.mark.parametrize(
    "command",
    [
        "cat .env.example",
        "cat .env.dist",
        "cat ~/.ssh/id_rsa.pub",
        "grep -c TOKEN .env",
        "grep -l TOKEN .env",
        "grep -q TOKEN .env",
        "grep -ic token .env",
        "grep --count TOKEN .env",
        "cat .env | grep -c TOKEN",
        "cat .env | wc -l",
        "grep -r '.env' src",
        "grep .env README.md",
        "awk '/.env/' README.md",
        "sed -i 's/a/b/' .env",
        "ls -la .env",
        "cat README.md",
        "cp .env.example .env",
        "echo .env",
    ],
)
def test_harmless_commands_on_secret_files_are_allowed(command):
    assert rule(command) is None


def test_a_configured_safe_filter_allows_the_read(tmp_path):
    filtered = "cat .env | grep -v -i -E 'token|password|secret'"
    home = guard_config(tmp_path, {"safe_filters": [r"^grep -v -i -E \S*token\S*$"]})
    assert rule(filtered) == "secret-read"
    assert rule(filtered, HARNESS_HOME=home) is None
    assert rule("cat .env | grep -v -i other", HARNESS_HOME=home) == "secret-read"


def test_configured_secret_paths_are_matched(tmp_path):
    home = guard_config(tmp_path, {"secret_globs": ["deploy/ci-secrets.yml", "*.secret"]})
    assert rule("cat deploy/ci-secrets.yml", HARNESS_HOME=home) == "secret-read"
    assert rule("cd deploy && cat ci-secrets.yml", HARNESS_HOME=home) == "secret-read"
    assert rule("cat db.secret", HARNESS_HOME=home) == "secret-read"
    assert rule("cat other/ci-secrets.yml", HARNESS_HOME=home) is None
    assert rule("cat .env", HARNESS_HOME=home) == "secret-read"


PROD = {"prod_db_hosts": [r"prod-db\.example\.com", r"^db-prod"]}


@pytest.mark.parametrize(
    "command",
    [
        "mysql -h prod-db.example.com shop",
        "mysql -hprod-db.example.com",
        "mysql --host=prod-db.example.com",
        "mariadb --host PROD-DB.example.com",
        "psql -h db-prod-1 -U app",
        "psql postgresql://app@prod-db.example.com:5432/shop",
        "psql 'host=prod-db.example.com dbname=shop'",
        "PGHOST=db-prod-1 psql",
        "mongosh mongodb://prod-db.example.com/shop",
        "redis-cli -h prod-db.example.com",
        "redis-cli -u redis://prod-db.example.com:6379",
        "bash -c 'mysql -h prod-db.example.com'",
    ],
)
def test_production_database_connections_are_denied(tmp_path, command):
    assert rule(command, HARNESS_HOME=guard_config(tmp_path, PROD)) == "prod-db"


@pytest.mark.parametrize(
    "command",
    [
        "mysql -h 127.0.0.1 shop",
        "psql -h localhost",
        "mysql shop",
        "mysql -h staging-db.example.com",
        "psql -h db-staging",
    ],
)
def test_other_database_hosts_are_allowed(tmp_path, command):
    assert rule(command, HARNESS_HOME=guard_config(tmp_path, PROD)) is None


def test_no_database_host_counts_as_production_by_default():
    assert rule("mysql -h prod-db.example.com shop") is None


@pytest.mark.parametrize(
    "command",
    [
        "mysql -e 'DROP DATABASE shop'",
        "mysql -h 127.0.0.1 -e 'drop table orders'",
        "mysql --execute='DROP SCHEMA shop'",
        "psql -c 'TRUNCATE orders'",
        "psql -tAc 'truncate table orders'",
        "mysql shop <<< 'DROP TABLE orders'",
        "mysql shop <<'SQL'\nDROP TABLE orders;\nSQL",
    ],
)
def test_destructive_sql_is_denied_for_any_host(command):
    assert rule(command) == "destructive-sql"


@pytest.mark.parametrize(
    "command",
    [
        "mysql -e 'SELECT TRUNCATE(price, 2) FROM products'",
        "mysql -e 'SELECT 1'",
        "psql -c 'SELECT count(*) FROM orders'",
        "echo 'DROP TABLE orders'",
    ],
)
def test_other_sql_is_allowed(command):
    assert rule(command) is None


def test_extra_deny_rules_use_their_reason(tmp_path):
    home = guard_config(
        tmp_path,
        {"extra_deny": [{"pattern": r"terraform\s+destroy", "reason": "Ask the person."}]},
    )
    reason = decide("cd infra && terraform destroy", HARNESS_HOME=home)
    assert reason == (
        "agent-harness guard, rule extra_deny: Ask the person. "
        "To proceed: change the command or ask the person."
    )
    assert decide("terraform plan", HARNESS_HOME=home) is None


def test_without_a_working_directory_only_absolute_paths_can_be_checked():
    event = {"tool_name": "Bash", "agent_type": SUBAGENT}
    for command, denied in (("rm -rf build", True), (f"rm -rf {SCRATCH}/x", False)):
        verdict = guard_shell.decision({**event, "tool_input": {"command": command}}, ENV)
        assert (verdict is not None) == denied


def nested_shells(levels: int, command: str) -> str:
    for _ in range(levels):
        command = f"bash -c {shlex.quote(command)}"
    return command


def test_shells_nested_too_deep_to_check_are_denied():
    assert rule(nested_shells(guard_shell.MAX_DEPTH, "ls")) is None
    assert rule(nested_shells(guard_shell.MAX_DEPTH, "rm -rf build")) == "rm-outside-allowed-dirs"
    assert rule(nested_shells(guard_shell.MAX_DEPTH + 1, "ls")) == "nesting"


def test_the_main_agent_is_not_checked_by_default():
    assert rule("rm -rf build", agent=None) is None


def test_scope_all_checks_the_main_agent_too(tmp_path):
    home = guard_config(tmp_path, {"scope": "all"})
    assert rule("rm -rf build", agent=None, HARNESS_HOME=home) == "rm-outside-allowed-dirs"


def test_other_tools_are_not_checked():
    event = {"tool_name": "Write", "tool_input": {"command": "rm -rf /"}, "agent_type": SUBAGENT}
    assert guard_shell.decision(event, ENV) is None


def test_every_deny_reason_names_the_rule_and_a_way_forward(tmp_path):
    home = guard_config(tmp_path, PROD)
    commands = {
        "force-push": "git push --force",
        "force-with-lease": "git push --force-with-lease",
        "git-discard": "git reset --hard",
        "rm-outside-allowed-dirs": "rm -rf build",
        "secret-read": "cat .env",
        "prod-db": "psql -h db-prod-1",
        "destructive-sql": "mysql -e 'DROP TABLE orders'",
    }
    for expected, command in commands.items():
        reason = decide(command, HARNESS_HOME=home)
        assert reason is not None
        assert reason.startswith(f"agent-harness guard, rule {expected}: ")
        assert re.fullmatch(r".+\. To proceed: [a-z].+\.", reason.split(": ", 1)[1]), reason


def guard_config(tmp_path: Path, settings: Any, name: str = "guard.json") -> str:
    home = tmp_path / "harness-home"
    home.mkdir(exist_ok=True)
    (home / name).write_text(settings if isinstance(settings, str) else json.dumps(settings))
    return str(home)


def project_config(tmp_path: Path, data: Any) -> Path:
    project = tmp_path / "shop"
    (project / ".claude").mkdir(parents=True, exist_ok=True)
    text = data if isinstance(data, str) else json.dumps(data)
    (project / ".claude" / "agent-harness.json").write_text(text)
    return project


def test_the_project_config_extends_lists_and_overrides_values(tmp_path):
    home = guard_config(tmp_path, {"scope": "all", "secret_globs": ["*.secret"]})
    project = project_config(
        tmp_path, {"guard": {"scope": "subagents", "secret_globs": ["vault.yml"]}}
    )
    env = {"HARNESS_HOME": home, "CLAUDE_PROJECT_DIR": str(project)}
    for file in ("db.secret", "vault.yml", ".env"):
        assert rule(f"cat {file}", cwd=str(project), **env) == "secret-read"
    assert rule("cat db.secret", agent=None, cwd=str(project), **env) is None


def test_the_example_config_is_valid(tmp_path):
    example = json.loads((ROOT / "examples" / "agent-harness.json").read_text())
    project = project_config(tmp_path, example)
    env = {"CLAUDE_PROJECT_DIR": str(project)}
    assert guard_shell.load_config(env, project).warnings == []
    assert rule("rm -rf node_modules", cwd=str(project), **env) is None
    assert rule("psql -h prod-db.internal", cwd=str(project), **env) == "prod-db"
    assert rule("cat deploy/ci-secrets.yml", cwd=str(project), **env) == "secret-read"
    filtered = "cat .env | grep -v -i -E 'token|password|secret'"
    assert rule(filtered, cwd=str(project), **env) is None
    assert rule("terraform apply", cwd=str(project), **env) == "extra_deny"


def test_relative_allowed_dirs_are_relative_to_the_project(tmp_path):
    project = project_config(tmp_path, {"guard": {"allowed_rm_dirs": ["var/cache"]}})
    env = {"CLAUDE_PROJECT_DIR": str(project)}
    assert rule("rm -rf var/cache/prod", cwd=str(project), **env) is None
    assert rule("rm -rf var/cache", cwd=str(project), **env) is None
    assert rule("rm -rf var", cwd=str(project), **env) == "rm-outside-allowed-dirs"
    assert rule("rm -rf var/log", cwd=str(project), **env) == "rm-outside-allowed-dirs"
    assert rule(f"rm -rf {project}/var/cache/dev", cwd="/work", **env) is None


@pytest.mark.parametrize(
    ("settings", "warning"),
    [
        ("{not json", "is not valid JSON"),
        (["scope"], "is not a JSON object"),
        ({"scope": "everyone"}, 'scope must be "subagents" or "all"'),
        ({"allow_force_with_lease": 1}, "allow_force_with_lease must be true, false"),
        ({"secret_globs": ".env"}, "secret_globs must be a list"),
        ({"secret_globs": [1]}, "secret_globs needs strings"),
        ({"prod_db_hosts": ["("]}, "prod_db_hosts needs valid regular expressions"),
        ({"extra_deny": [{"pattern": "x"}]}, "extra_deny needs"),
        ({"secret_glob": ["x"]}, "unknown key secret_glob"),
    ],
)
def test_invalid_config_falls_back_to_the_defaults_with_a_warning(tmp_path, settings, warning):
    home = guard_config(tmp_path, settings)
    reason = decide("cat .env", HARNESS_HOME=home)
    assert reason is not None
    assert reason.startswith("agent-harness guard, rule secret-read: ")
    assert "Guard config warnings: " in reason
    assert warning in reason
    assert decide("cat README.md", HARNESS_HOME=home) is None


@pytest.mark.parametrize("data", ["{not json", '["guard"]', '{"guard": "all"}'])
def test_an_invalid_project_config_is_reported(tmp_path, data):
    project = project_config(tmp_path, data)
    reason = decide("rm -rf build", CLAUDE_PROJECT_DIR=str(project))
    assert reason is not None
    assert "Guard config warnings: " in reason


def run_hook(stdin: str, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        [sys.executable, str(SCRIPT)],
        input=stdin,
        capture_output=True,
        text=True,
        env={**os.environ, **ENV, "HARNESS_HOME": str(tmp_path), "CLAUDE_PROJECT_DIR": ""},
        check=False,
    )


def test_the_hook_prints_the_denial(tmp_path):
    event = {"tool_name": "Bash", "tool_input": {"command": "rm -rf /"}, "agent_type": SUBAGENT}
    result = run_hook(json.dumps(event), tmp_path)
    assert result.returncode == 0
    output = json.loads(result.stdout)["hookSpecificOutput"]
    assert output["permissionDecision"] == "deny"


@pytest.mark.parametrize("stdin", ["not json", "[]", json.dumps({"tool_name": "Bash"})])
def test_bad_input_allows_the_command(tmp_path, stdin):
    result = run_hook(stdin, tmp_path)
    assert result.returncode == 0
    assert result.stdout == ""


def test_an_internal_error_allows_the_command_and_logs_it(tmp_path):
    result = run_hook("not json", tmp_path)
    assert "guard_shell.py allowed the command after an internal error" in result.stderr


def test_hooks_json_runs_the_guard_on_bash():
    config = json.loads((ROOT / "hooks" / "hooks.json").read_text())
    [group] = [g for g in config["hooks"]["PreToolUse"] if g["matcher"] == "Bash"]
    [handler] = group["hooks"]
    assert handler["command"].endswith('/hooks/guard_shell.py"')
    assert handler["timeout"] == 10
