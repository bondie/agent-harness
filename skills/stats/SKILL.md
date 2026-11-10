---
name: stats
description: Show how efficient the subagents are, from the run log the plugin writes after every finished subagent run - per agent the runs, typical duration, API requests, tool calls and tokens, and how the last week compares with the week before. Use when the person asks how the agents perform, whether they got faster or cheaper, before and after changing an agent, or between plugin versions or models, e.g. "/agent-harness:stats" or "/agent-harness:stats --by version".
---

# Agent stats

The plugin's `SubagentStop` hook appends one line per finished subagent run to
`~/.claude/agent-harness/runs.jsonl` (`HARNESS_RUNS_LOG` overrides the path). A resumed
background agent logs again with cumulative totals, so keep the last line per `agent_id`.

Run this and show the person the table, then two or three observations: the agent that costs
the most, anything that got slower or more expensive week over week, and runs far above the
usual (they are worth a look in the retro). An optional argument filters by project or agent.

Two more arguments compare groups of runs instead of weeks, per agent with the same medians:

- `--by version` or `--by model` groups each agent's runs by the plugin version or the model
  they ran with, in the order the groups first ran. Lines logged before the plugin recorded
  its version form the group `unknown`.
- `--split <ISO date or datetime>` (`2026-11-08` or `2026-11-08T14:30`, no space; local time
  unless it has a zone) compares the runs that ended before that moment with the ones that
  ended at or after it, and shows the relative change. Give it the time of a plugin update to
  compare two versions while the older lines carry no version; sessions already running at
  the update may still log under the old one.

With either, say where the numbers moved and how many runs stand behind each group: a median
of two runs proves little.

`--memory`, alone or with the others, adds the column `mem kc in/rd/sh`: per agent, the median
thousands of characters of its own memory in a run, as the index in its prompt / topic files
it opened with Read (without a read that an edit of the file follows within 3 calls) / output of
Bash commands that name its memory directory (an upper bound). A `-` stands for runs without
the fields: logged before the plugin recorded memory, or of an agent that has no memory.

```bash
python3 - "${ARGUMENTS:-}" <<'EOF'
import json, os, statistics, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

path = Path(os.environ.get("HARNESS_RUNS_LOG") or Path.home() / ".claude/agent-harness/runs.jsonl")
if not path.is_file():
    sys.exit(f"No runs logged yet ({path}).")
args, words = " ".join(sys.argv[1:]).split(), []
by = split = None
memory = False
while args:
    arg = args.pop(0)
    if arg == "--memory":
        memory = True
        continue
    if arg in ("--by", "--split") and not args:
        sys.exit(f"{arg} needs a value.")
    if arg == "--by":
        by = args.pop(0)
        continue
    if arg == "--split":
        split = args.pop(0)
        continue
    words.append(arg)
wanted = " ".join(words)
if by not in (None, "version", "model"):
    sys.exit(f"--by takes version or model, not {by}.")
if by and split:
    sys.exit("Use --by or --split, not both.")
try:
    moment = datetime.fromisoformat(split.replace("Z", "+00:00")).astimezone() if split else None
except ValueError:
    sys.exit(f"--split takes an ISO date or datetime, not {split}.")
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
by_type = {}
for run in runs.values():
    by_type.setdefault(run.get("agent_type") or "?", []).append(run)
agents = sorted(by_type.items(), key=lambda kv: -len(kv[1]))
def medians(items):
    return [med([r.get("seconds") for r in items]) / 60, med([r.get("requests") for r in items]),
            med([r.get("tool_uses") for r in items]), med([(r.get("tokens") or {}).get("output_tokens") for r in items])]
def mem(items):
    cells = []
    for field in ("memory_injected_chars", "memory_read_chars", "memory_bash_chars"):
        values = [r[field] for r in items if type(r.get(field)) in (int, float)]
        cells.append(f"{statistics.median(values) / 1000:.1f}" if values else "-")
    return "-" if cells == ["-"] * 3 else "/".join(cells)
def extra(cell):
    return f" {cell:>15}" if memory and cell else ""
def row(agent, group, count, cells, memory_cell=""):
    print(f"{agent[:32]:32} {group[:24]:24} {count:>4} {cells[0]:>8} {cells[1]:>8} {cells[2]:>9} {cells[3]:>11}" + extra(memory_cell))
def group_of(run):
    if moment:
        return "after" if when(run) >= moment else "before"
    return run.get("plugin_version" if by == "version" else "model") or "unknown"
if by or moment:
    row("agent", by or "split", "runs", ["med min", "med req", "med tools", "med out tok"], "mem kc in/rd/sh")
    for agent, items in agents:
        groups = {}
        for run in sorted(items, key=when):
            groups.setdefault(group_of(run), []).append(run)
        for group, members in groups.items():
            values = medians(members)
            row(agent, group, len(members), [f"{values[0]:.1f}", *(f"{v:.0f}" for v in values[1:])], mem(members))
        if not moment or len(groups) < 2:
            continue
        before, after = medians(groups["before"]), medians(groups["after"])
        row(agent, "change", "", [f"{a / b - 1:+.0%}" if b else "-" for a, b in zip(after, before)])
    sys.exit()
print(f"{'agent':32} {'runs':>4} {'med min':>8} {'med req':>8} {'med tools':>9} {'med out tok':>11} {'all tok (M)':>11} {'7d vs prev':>11}" + extra("mem kc in/rd/sh"))
for agent, items in agents:
    total = sum(sum((r.get("tokens") or {}).values()) for r in items) / 1e6
    week = [r["seconds"] for r in items if r.get("seconds") and when(r) > now - timedelta(days=7)]
    prev = [r["seconds"] for r in items if r.get("seconds") and now - timedelta(days=14) < when(r) <= now - timedelta(days=7)]
    trend = f"{med(week) / med(prev) - 1:+.0%}" if week and prev else "-"
    print(f"{agent[:32]:32} {len(items):>4} {med([r.get('seconds') for r in items]) / 60:>8.1f} "
          f"{med([r.get('requests') for r in items]):>8.0f} {med([r.get('tool_uses') for r in items]):>9.0f} "
          f"{med([(r.get('tokens') or {}).get('output_tokens') for r in items]):>11.0f} {total:>11.1f} {trend:>11}" + extra(mem(items)))
EOF
```

Durations are wall-clock time from the first to the last line of the agent's transcript; a
background agent that waits on a pipeline is slow by design, so compare an agent only with
itself. "all tok" adds input, output and cache tokens; cache reads are cheap, so look at the
output tokens and the requests for cost.
