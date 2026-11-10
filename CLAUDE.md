# agent-harness

A Claude Code plugin (four subagents, the `ticket`, `retro` and `stats` skills, a dynamic
workflow, ten hook scripts, an MCP server entry) and the Python package that backtests its
estimator and measures the team.

## Layout

- `agents/*.md`: the subagents. `agents/estimator.md` is also the system prompt of the LLM
  variants in the evaluation, so a change to it changes the measured results: rerun the eval.
- `skills/ticket/SKILL.md`: the team-lead flow with the two approval gates;
  `skills/retro/SKILL.md` turns a finished ticket into approved memory lessons;
  `skills/stats/SKILL.md` summarises the run log.
- `workflows/*.js`: dynamic workflows shipped with the plugin (`/agent-harness:<name>`). Plain
  JavaScript, `meta` a pure literal, no clock or randomness; `tests/test_workflows.py` checks it.
- `hooks/`: standard-library Python scripts called by `hooks/hooks.json`: the tracker, memory
  and shell guards (PreToolUse), `guard_runaway.py` (PreToolUse, PostToolUse and
  PostToolUseFailure on every tool), `run_tests.py` (PostToolUse), `log_agent_runs.py`,
  `save_artifacts.py`, `quality_gate.py`, `estimate_check.py` and `memory_cap.py`
  (SubagentStop). The last three also run as PreToolUse on `SubagentHandback`, where auto mode
  delivers a report before the subagent stops; both paths share one block count per run.
  `memory_cap.py` also runs on SubagentStart, where it records the sizes it measures growth from.
  Keep them free of third-party imports, they run with the user's `python3`; they may import
  each other, except `estimate_check.py`, which `src/agent_harness/estimate_check.py` copies
  byte for byte.
- `src/agent_harness/`: the backtest (`harness-eval`), which reads a ticket-precedent index,
  and the team measures (`export-case`, `eval`, `metrics`, `review-finding`, `escaped`), which
  read the ticket artifacts. The hooks cannot import it, so what both need is duplicated and
  tested to stay in step: `memory.py` (`harness-eval memory`) parses memory lessons like
  `hooks/guard_memory_writes.py` (`tests/test_memory.py`), `artifacts.py` parses report blocks
  like `hooks/save_artifacts.py` (`tests/test_team_cli.py`), and `tests/test_harness_home.py`
  keeps the `HARNESS_HOME` default the same everywhere.
- `results/`: published aggregate reports. `runs/` holds per-ticket results and is ignored.

## Rules

- Public repository: no real ticket keys, client or project names, URLs or ticket text in
  code, tests, docs, results or commit messages. Reports name projects "Project A, B, …".
- Plugin agents ignore `hooks`, `mcpServers` and `permissionMode` in their front matter; put
  hooks in `hooks/hooks.json` and filter on `agent_type`.
- Metrics are unit-tested against definitions computed by brute force; keep it that way.

## Commands

```bash
uv sync
uv run pytest --cov
uv run ruff check . && uv run ruff format --check . && uv run mypy
claude plugin validate .
uv run harness-eval run --out runs/NAME --from YYYY-MM-DD --to YYYY-MM-DD [--platform P] [--variants …]
uv run harness-eval report runs/NAME
```
