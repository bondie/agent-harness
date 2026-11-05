# agent-harness

A Claude Code plugin (four subagents, a `ticket` skill, two hooks, an MCP server entry) and the
Python package that backtests its estimator.

## Layout

- `agents/*.md`: the subagents. `agents/estimator.md` is also the system prompt of the LLM
  variants in the evaluation, so a change to it changes the measured results: rerun the eval.
- `skills/ticket/SKILL.md`: the team-lead flow with the two approval gates.
- `hooks/`: standard-library Python scripts called by `hooks/hooks.json`. Keep them free of
  third-party imports, they run with the user's `python3`.
- `src/agent_harness/`: the backtest (`harness-eval`). Reads a ticket-precedent index.
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
uv run harness-eval run --out runs/NAME --from YYYY-MM-DD --to YYYY-MM-DD [--variants …]
uv run harness-eval report runs/NAME
```
