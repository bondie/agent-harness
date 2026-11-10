---
name: retro
description: After a ticket is done, compare the estimate and the plan with what actually happened, have each agent of the team propose lessons for its memory and write only the ones the person approves, so the next estimate, plan, implementation and review get better. Use when the person asks for a retro or a lessons-learned pass on a ticket, e.g. "/agent-harness:retro SHOP-412", or when the ticket playbook offers it at the end.
---

# Retro: teach the team from one finished ticket

You are the team lead. The agents learn only through their memory, and only from facts: give
each agent the ones that concern it and let the person decide what it keeps. Ticket key: the
argument (ask if missing). Run it once the time is logged; half-logged time misleads.

## 1. Collect the facts

Start from `~/.claude/agent-harness/tickets/<KEY>/` (under `$HARNESS_HOME` when set): the
reports (`<agent>.<NN>.md`, listed in `index.jsonl`), the structured blocks (`analysis.json`,
`estimate.json`, `review.json`, `implementation.json`) and `gates.json`. Each review round
leaves its own block: `review.json` is the last round, and the earlier ones are kept as
`review.01.json`, `review.02.json`, … in order. Take from the conversation only what is
missing there.

- **Estimate:** the estimator's hours, per part when bottom-up (`estimate.json`).
- **Actual time:** the person's logged hours on the ticket from the tracker's worklogs, and the
  total if others logged too. Ask the person when the worklogs are not readable.
- **Size:** per repository the ticket touched, `git log --all --author=<person> --grep=<KEY>
  --numstat`: commits, files, lines added and removed, and how many added lines are tests.
- **Plan against reality:** the files in `analysis.json` against the files that changed.
- **Implementation:** its "Where the time went", failing runs, steps the lead did by hand.
- **Review:** each finding of every round (`review*.json`) and whether it was accepted,
  rejected or deferred. Finding ids restart in each round: F1 of round 1 and F1 of round 2
  are two findings.
- **Corrections:** anything the person said about an agent's output during the ticket.

Use numbers, not impressions; say when a fact is missing. Then keep the numbers with
`harness-eval`, the plugin's Python command. A plugin install does not put it on PATH: run it
as `uvx --from git+https://github.com/bondie/agent-harness harness-eval …`, or as
`uv run --project <an agent-harness checkout> harness-eval …`. Without `uv`, skip this step
and say so in the report.

- Each finding's disposition: `harness-eval review-finding <KEY> F1 … --disposition
  accepted|rejected|deferred`, with `--round N` when the id is in more than one round.
- The eval case: `harness-eval export-case <KEY> --source worklog --actual-total <hours from
  the worklogs> --repo <paths>`. Add `--actual-part implementation=…` (or `verification`,
  `release`) only for a part whose time was measured, with `--source measured` when all of
  it was; never split the logged total into parts by guess.
- The memory size: `harness-eval memory lint` (README, "Memory hygiene") lists per agent the
  files near the cap (at `warn_ratio` of it, 80 % by default) and over it, with the share.

## 2. Have each agent propose its lessons

Run the agents in parallel, each with a task that starts with `Retro for <KEY>`: "Retro for
<KEY>. Do not write to your memory yet, and end without a structured block. From the
following, propose the lines to add, each as `- <lesson> (source: <KEY>, <today>[, expires
YYYY-MM-DD])`, and the existing lines to change or delete. For each memory file listed below
as near or over its cap, also propose verbatim moves: which whole lessons, wording and source
unchanged, go into which topic file linked from MEMORY.md, and the pointer line MEMORY.md
needs; when its lessons cannot bring it under the mark, propose merging topics or shortening
pointers." The plugin's hooks recognise a retro run by that start and do not ask it for a
block. Use `retro` as the source for a lesson that outgrows the ticket, and an expiry for one
tied to a version, release or incident.
Send each only what concerns it, and skip an agent with nothing new:

- **estimator:** a calibration row (repositories, files, lines with tests, the person's
  hours), the estimate against the actual time, and which part was off and why.
- **jira-analyst:** what the plan missed or overplanned, and traps found in the codebase.
- **implementer:** where the time went and the commands or conventions that would have saved it.
- **reviewer-qa:** the findings the person rejected and why, and defects that got through.
- **every agent with a file near or over its cap:** those files from the lint in step 1, each
  with its size, its cap and the warning mark (`warn_ratio`), so the moves land in the list of
  step 3.

## 3. Let the person decide

Show the proposed lines and moves per agent, numbered, to approve, edit or reject each.
Nothing goes into memory before that: a wrong lesson misleads every later run, and a
remembered rejection can hide the next real finding.

## 4. Write the approved lines

Send each agent with approved lines or moves, in its retro run from step 2 (SendMessage to that
agent): "Write these approved lines to your memory exactly as given, make these approved
verbatim moves, and make these approved changes and deletions: …". If you start a new run
instead, begin its task with `Retro for <KEY>` too, so the hooks treat it as a retro. A hook
denies a line without source and date and a file over its size cap. A verbatim move, whole
lessons with the same wording and the same `(source: …)` into a topic file linked from
`MEMORY.md`, is not a compaction: outside a retro the agent makes it itself and lists the moved
lines in its report; in a retro run it proposes the move in its report, with the lines and the
target file, for the person to approve. Rewording, merging or deleting a lesson always goes
through the person.

## 5. Report

Show the estimate against the actual time, the biggest lesson per agent and the lines
written. When the hook asked an agent to propose a move while it wrote in step 4, its report
holds the proposal: show it to the person, numbered as in step 3, and send the approved moves
as in step 4. After every retro, run `harness-eval memory lint` again, as in step 1, and show
the files still near or over the cap. A retro stays local: never write it to the tracker unless
the person asks for it.
