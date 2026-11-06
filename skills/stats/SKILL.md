---
name: stats
description: Show how efficient the subagents are, from the run log the plugin writes after every finished subagent run - per agent the runs, typical duration, API requests, tool calls and tokens, and how the last week compares with the week before. Use when the person asks how the agents perform, whether they got faster or cheaper, or before and after changing an agent, e.g. "/agent-harness:stats".
---

# Agent stats

The plugin's `SubagentStop` hook appends one line per finished subagent run to
`~/.claude/agent-harness/runs.jsonl` (`HARNESS_RUNS_LOG` overrides the path). A resumed
background agent logs again with cumulative totals, so keep the last line per `agent_id`.

Run this and show the person the table, then two or three observations: the agent that costs
the most, anything that got slower or more expensive week over week, and runs far above the
usual (they are worth a look in the retro). An optional argument filters by project or agent.

```bash
python3 - "${ARGUMENTS:-}" <<'EOF'
import json, os, statistics, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

path = Path(os.environ.get("HARNESS_RUNS_LOG") or Path.home() / ".claude/agent-harness/runs.jsonl")
if not path.is_file():
    sys.exit(f"No runs logged yet ({path}).")
wanted = sys.argv[1] if len(sys.argv) > 1 else ""
runs = {}
for line in path.read_text().splitlines():
    try:
        run = json.loads(line)
    except ValueError:
        continue
    if wanted and wanted not in (run.get("project") or "") and wanted not in (run.get("agent_type") or ""):
        continue
    runs[run.get("agent_id") or line] = run
now = datetime.now(timezone.utc)
def when(run):
    return datetime.fromisoformat((run.get("ended") or "1970-01-01T00:00:00Z").replace("Z", "+00:00"))
def med(values):
    values = [v for v in values if v is not None]
    return statistics.median(values) if values else 0
print(f"{'agent':32} {'runs':>4} {'med min':>8} {'med req':>8} {'med tools':>9} {'med out tok':>11} {'all tok (M)':>11} {'7d vs prev':>11}")
by_type = {}
for run in runs.values():
    by_type.setdefault(run.get("agent_type") or "?", []).append(run)
for agent, items in sorted(by_type.items(), key=lambda kv: -len(kv[1])):
    total = sum(sum((r.get("tokens") or {}).values()) for r in items) / 1e6
    week = [r["seconds"] for r in items if r.get("seconds") and when(r) > now - timedelta(days=7)]
    prev = [r["seconds"] for r in items if r.get("seconds") and now - timedelta(days=14) < when(r) <= now - timedelta(days=7)]
    trend = f"{med(week) / med(prev) - 1:+.0%}" if week and prev else "-"
    print(f"{agent[:32]:32} {len(items):>4} {med([r.get('seconds') for r in items]) / 60:>8.1f} "
          f"{med([r.get('requests') for r in items]):>8.0f} {med([r.get('tool_uses') for r in items]):>9.0f} "
          f"{med([(r.get('tokens') or {}).get('output_tokens') for r in items]):>11.0f} {total:>11.1f} {trend:>11}")
EOF
```

Durations are wall-clock time from the first to the last line of the agent's transcript; a
background agent that waits on a pipeline is slow by design, so compare an agent only with
itself. "all tok" adds input, output and cache tokens; cache reads are cheap, so look at the
output tokens and the requests for cost.
