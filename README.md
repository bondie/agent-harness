# agent-harness

[![CI](https://github.com/bondie/agent-harness/actions/workflows/ci.yml/badge.svg)](https://github.com/bondie/agent-harness/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-blue)
![License](https://img.shields.io/badge/license-MIT-green)

**A ticket team for Claude Code, with an estimator you can check.** Four subagents take a
tracker ticket from analysis to a reviewed change: an analyst, an estimator, an implementer
and a reviewer. A person approves the plan and the estimate before any code is written, and
nothing is written to the tracker without their confirmation.

The estimator does not guess. It looks up how long similar finished tickets actually took
([reference class forecasting](https://en.wikipedia.org/wiki/Reference_class_forecasting))
through [ticket-precedent](https://github.com/bondie/ticket-precedent), and answers with a
median, a P25–P75 range and the tickets it relied on. The repository also contains the
backtest that measures it against simpler baselines, a local model and human estimates on
real, private ticket history; only aggregate numbers are published.

**Backtest on 1,212 finished tickets from 2026** (private history, logged hours as ground
truth, no future data in reach): the estimator with a recent reference class is as accurate
as the plain median of similar recent tickets (MAE 1.36 h vs 1.34 h, p = 0.78) and its
P25–P75 range holds the actual time 52% of the time. Without similar tickets, Claude Sonnet is
worse than random guessing and a local 12B model is far worse; with them, the local model
matches Claude. The biggest gain came from neither model: limiting the reference class to
tickets after a process change cut the error by 29%. [Details](#does-it-work).

## What is inside

It is a Claude Code [plugin](https://code.claude.com/docs/en/plugins), and the repository is
its own marketplace.

| Component | Role |
|---|---|
| `agent-harness:jira-analyst` | Reads the ticket, comments, links and precedents, finds the code; returns problem, acceptance criteria, approach, open questions. Read-only. |
| `agent-harness:estimator` | Reference class forecast in hours: median, P25–P75, cited tickets, named adjustments. Read-only. |
| `agent-harness:implementer` | Implements the approved approach test-first, inside its scope. Never commits or writes to the tracker. |
| `agent-harness:reviewer-qa` | Reviews the diff against the acceptance criteria, runs the checks, reports `path:line` findings and a verdict. Does not edit. |
| `/agent-harness:ticket KEY` | The team lead's playbook for the main session, with the two approval gates. |
| `hooks/run_tests.py` | After each edit **by the implementer**, runs the project's tests and hands failures back to it. |
| `hooks/guard_tracker_writes.py` | Denies tracker writes (comments, transitions, edits) to every subagent. |
| `.mcp.json` | Starts the ticket-precedent MCP server. |

```mermaid
flowchart LR
    U([Person]) -->|/agent-harness:ticket SHOP-412| L[Team lead<br/>main session]
    L --> A[jira-analyst] --> E[estimator]
    E --> G1{Gate 1<br/>approve plan<br/>and estimate}
    G1 -->|implement| I[implementer]
    I -->|edit| H[[test hook]] --> I
    I --> R[reviewer-qa]
    R -->|changes requested| I
    R --> G2{Gate 2<br/>commit? comment?}
    G1 -->|estimate only| G2
    G2 -->|confirmed| T[(Tracker / git)]
```

## Install

```text
/plugin install agent-harness --marketplace bondie/agent-harness
```

Then, per project:

1. **An index of past tickets.** Set up [ticket-precedent](https://github.com/bondie/ticket-precedent)
   with your tracker and run `tp sync`. The plugin's `.mcp.json` starts its MCP server with
   `uvx`; if you already registered ticket-precedent yourself, remove one of the two.
2. **A tracker MCP server** of your choice (JIRA, GitHub, …) for the analyst to read tickets.
3. **The test command** for the hook, in the project's `.claude/settings.json` (see
   [`examples/settings.json`](examples/settings.json)):

   ```json
   { "env": { "HARNESS_TEST_COMMAND": "uv run pytest -x -q", "HARNESS_TEST_TIMEOUT": "300" } }
   ```

   `{file}` in the command becomes the edited file, e.g. `phpunit --filter … {file}`. Without
   the variable the hook does nothing.

## The estimator

The method is in [`agents/estimator.md`](agents/estimator.md):

1. Get the reference class: `effort_reference` returns the most similar tickets **whose time
   was all logged before the ticket was created**, with their hours, median and P25–P75.
2. Discard neighbours that only share vocabulary; open the closest ones when in doubt.
3. Anchor on the median, then adjust only for differences it can name.
4. Keep the P25–P75 range honest: it should contain the actual time half of the time.
5. Prefer neighbours from after a process change (or scale older ones down).

It never reads the ticket's existing estimate or logged time: a model shown a number anchors
on it ([Çalikli & Alhamed, FSE 2025](https://doi.org/10.1145/3715771)), and the person
comparing estimates needs an independent one.

## Does it work?

Full tables: [`results/2026-backtest.md`](results/2026-backtest.md).

**Setup.** The tickets of eight private JIRA projects with logged time, created in 2026 and
done, up to 40 h (1,212 tickets; the LLM variants ran on a random 150 of them, seed 7). Ground
truth is the logged time. For each ticket, only tickets whose time was all logged before it
was created can be retrieved. The LLM variants use the shipped estimator prompt, run
headless, with numbers next to estimate words in the ticket text masked (16 of 150 tickets).
At the start of 2026 the team began implementing with AI coding agents, and the same work
now logs much less time: the median logged time fell from 1.6–2.0 h (2022–2025) to 1.0 h.

| Variant (150 tickets) | MAE h [95% CI] | MdAE h | SA | P25–P75 coverage |
|---|---:|---:|---:|---:|
| Median of 10 similar tickets, all history | 1.89 [1.58–2.23] | 1.30 | 0.11 | 46% |
| … older tickets scaled by the pace change | 1.44 [1.17–1.76] | 0.88 | 0.33 | 50% |
| **… only tickets after the change** | **1.34 [1.06–1.68]** | **0.75** | **0.37** | 49% |
| Claude Sonnet, ticket text only | 2.37 [1.91–2.89] | 1.50 | −0.11 | 43% |
| Claude Sonnet + keyword neighbours | 1.58 [1.28–1.92] | 1.00 | 0.26 | 59% |
| Claude Sonnet + ticket-precedent | 1.60 [1.30–1.94] | 1.00 | 0.25 | 50% |
| **Claude Sonnet + ticket-precedent, recent only** | **1.36 [1.11–1.68]** | 1.00 | 0.36 | **52%** |
| gemma4:12b (Ollama), ticket text only | 6.24 [4.53–8.31] | 3.04 | −1.92 | 19% |
| gemma4:12b + ticket-precedent | 1.57 [1.24–1.93] | 0.78 | 0.27 | 50% |
| **gemma4:12b + ticket-precedent, recent only** | **1.37 [1.09–1.72]** | 0.75 | 0.36 | 51% |

SA is standardized accuracy against random guessing (0 = no better than guessing). On all
1,212 tickets the medians tell the same story: 2.33 h all history, 1.86 h scaled, 1.66 h
recent only (all differences p < 0.001, in every project).

What the numbers say:

- **The reference class matters more than the model.** Dropping tickets from before the
  process change cut MAE by 29% for the plain median and by 15% for Claude. A model told
  about the change but shown old tickets still overestimated.
- **The LLM does not beat a good median on accuracy** (p = 0.78 for Claude, 0.98 for gemma
  against the recent median), as the estimation literature keeps finding. What it adds is a
  calibrated range, cited tickets and named adjustments that a person can check at the gate.
- **Without retrieval, LLM estimates are guesses.** Claude Sonnet was worse than random
  guessing; the 12B model overestimated by three hours at the median.
- **A local model is enough.** With the same reference class, gemma4:12b on a laptop matched
  Claude Sonnet (1.37 h vs 1.36 h) at no cost; Claude cost about 0.018 USD per estimate.
- **The prompt's anchoring rule is not followed.** It says to keep the median unless a
  difference can be named, yet both models moved away from it on 93–95% of tickets and ended
  up overestimating slightly more than the median itself (+0.25 to +0.50 h vs +0.31 h).
  That is the next thing to fix, and the backtest can measure the fix.
- **Keyword and hybrid retrieval tie here** (1.58 h vs 1.60 h for Claude). The tickets of one
  team share vocabulary, so BM25 finds the same neighbours.

**Human estimates and their catch.** The first estimate a person entered had an MAE of 0.56 h,
far better than any variant, but in 2026 the logged time equals that estimate exactly on
59% of tickets: time is logged to the estimate, so the comparison is circular. On the 479
tickets where logged time differs from the estimate, the human estimate still leads (MAE
1.32 h vs 1.87 h for the recent median), with the same caveat that logging may lean towards it.

**Threats to validity.** One organisation; logged time is a proxy for effort; descriptions
can be edited after creation; the 150-ticket sample gives wide intervals (about ±0.3 h);
the process note given to the LLMs names the change date, which a real estimator would also
be told.

## Design decisions

- **A plugin, not a project `.claude/` folder.** The same team works in every repository,
  including from the JetBrains and VS Code integrations, and updates in one place.
- **Hooks live in the plugin, filtered by agent.** Claude Code ignores `hooks`,
  `mcpServers` and `permissionMode` in the front matter of plugin agents, so the test hook is
  a plugin-level `PostToolUse` hook that checks `agent_type` and runs only for the
  implementer. An end-to-end run confirmed that it fires for the subagent's edits.
- **Tracker writes are blocked, not just discouraged.** The prompts say subagents only read;
  a `PreToolUse` hook enforces it for any MCP tool that looks like a tracker write. The main
  session keeps its normal permission prompt, and the playbook asks before every write.
- **Two human gates.** Between estimate and implementation (the expensive mistake is
  building the wrong thing) and before anything leaves the machine (commit, comment).
- **The eval runs the shipped prompt.** The LLM variants use the body of
  `agents/estimator.md` as their system prompt, so changing the agent changes the measured
  results.
- **Clean headless runs.** `claude -p` runs in an empty directory with no tools, no MCP
  servers and no settings (`--setting-sources ""`), so neither `CLAUDE.md` files nor hooks
  leak into the measurement. (`--bare` would do the same but requires an API key.)
- **Hours, not story points.** The trackers record logged hours, and story points correlate
  strongly with time in only 7% of the projects in the public TAWOS dataset
  ([Tawosi et al., ESEM 2022](https://doi.org/10.1145/3544902.3546238)).

## Prior art

- **Subagent teams:** [wshobson/agents `agent-teams`](https://github.com/wshobson/agents/tree/main/plugins/agent-teams)
  (lead, implementer, reviewer), [VoltAgent/awesome-claude-code-subagents](https://github.com/VoltAgent/awesome-claude-code-subagents)
  (conventions), [BMAD-METHOD](https://github.com/bmad-code-org/BMAD-METHOD) and
  [spec-kit](https://github.com/github/spec-kit) (human-reviewed artifacts between phases).
- **Estimation research:** Deep-SE and GPT2SP for story points, and their replications,
  which found that a plain median baseline is hard to beat and that temporal leakage
  inflated earlier results ([Tawosi et al., TSE 2023](https://arxiv.org/abs/2201.05401)).
  Hours from similar earlier issues go back to [Weiss et al., MSR 2007](https://doi.org/10.1109/MSR.2007.13).
- **LLM and RAG estimators:** retrieval-augmented story-point estimation was not
  significantly better than the baselines ([Maha et al., ICPC 2026](https://arxiv.org/abs/2604.03443));
  the choice of few-shot examples matters ([Tawosi et al., SSBSE 2023](https://arxiv.org/abs/2403.08430)).
  Existing Claude Code estimator agents (e.g. [ninja-agents `jira-story-points`](https://github.com/ninja-agents/ninja-agents/blob/main/.claude/agents/jira-story-points.md))
  are not evaluated.

What this repository adds: hours from private worklogs (no training-data contamination),
prediction ranges with measured coverage, a temporal cut-off inside the retrieval tool, a
baseline ladder that isolates what the LLM adds, human estimates, and a hosted and a local
model through the same pipeline.

## Development

```bash
uv sync                      # installs ticket-precedent from GitHub at a pinned commit
uv run pytest --cov          # no network, no model, no tracker needed
uv run ruff check . && uv run ruff format --check . && uv run mypy
claude plugin validate .
```

## License

[MIT](LICENSE)
