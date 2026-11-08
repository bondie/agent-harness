---
name: retro
description: After a ticket is done, compare the estimate and the plan with what actually happened and have each agent of the team record its lessons in its memory, so the next estimate, plan, implementation and review get better. Use when the person asks for a retro or a lessons-learned pass on a ticket, e.g. "/agent-harness:retro SHOP-412", or when the ticket playbook offers it at the end.
---

# Retro: teach the team from one finished ticket

You are the team lead. The agents learn only through their memory, and only from facts. Your
job is to collect the facts, give each agent the ones that concern it, and let it update its
own memory. Ticket key: the argument (ask for it if missing). Run it when the work is done and
its time is logged; a retro on half-logged time teaches the estimator the wrong numbers.

## 1. Collect the facts

Start from the files the plugin kept for the ticket in `$HARNESS_HOME/tickets/<KEY>/`
(default `~/.claude/agent-harness/tickets/<KEY>/`): each agent's report (`<agent>.<NN>.md`,
listed in `index.jsonl`), its structured block (`analysis.json`, `estimate.json`,
`review.json`, `implementation.json`) and the quality gate result (`gates.json`). Reconstruct
from the conversation only what is missing there.

- **Estimate:** what the estimator said (the hours, per part when it estimated
  bottom-up), from this conversation or from the comment where it was posted.
- **Actual time:** the person's own logged hours on the ticket from the tracker's worklogs,
  and the total if others logged too. Ask the person when the worklogs are not readable.
- **Size of the change:** for every repository the ticket touched, `git log --all
  --author=<person> --grep=<KEY> --numstat`: commits, files, lines added and removed, and how
  many of the added lines are tests.
- **Plan against reality:** the files in the analyst's implementation plan against the files
  that actually changed: what the plan missed, what it planned that was not needed.
- **Implementation:** the implementer's "Where the time went", failing test runs, steps the
  lead had to do by hand.
- **Review:** each finding and what the person did with it: fixed, rejected as noise, deferred.
- **Corrections:** anything the person said about an agent's output during the ticket.

Use numbers, not impressions. When a fact is missing, say so instead of guessing.

## 2. Tell each agent what concerns it

Run the agents in parallel, each with a short task: "Retro for <KEY>. Update your memory with
the following, then reply with the lines you added or changed." Send only what concerns that
agent, and skip an agent with nothing new:

- **estimator:** a calibration row (repositories, files, lines with tests, the person's hours),
  the estimate against the actual time, and which part was off and why.
- **jira-analyst:** what the plan missed or overplanned, and traps found in the codebase.
- **implementer:** where the time went and the commands or conventions that would have saved it.
- **reviewer-qa:** the findings the person rejected and why, and the defects that got through
  the review.

## 3. Report

Show the person, briefly: the estimate against the actual time, the biggest lesson per agent,
and the memory lines each agent added. A retro stays local: never write it to the tracker
unless the person asks for it.
