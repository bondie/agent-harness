---
name: retro
description: After a ticket is done, compare the estimate and the plan with what actually happened, have each agent of the team propose lessons for its memory and write only the ones the person approves, so the next estimate, plan, implementation and review get better. Use when the person asks for a retro or a lessons-learned pass on a ticket, e.g. "/agent-harness:retro SHOP-412", or when the ticket playbook offers it at the end.
---

# Retro: teach the team from one finished ticket

You are the team lead. The agents learn only through their memory, and only from facts. Your
job is to collect the facts, give each agent the ones that concern it, and let the person
decide what each agent keeps. Ticket key: the argument (ask for it if missing). Run it when the
work is done and its time is logged; a retro on half-logged time teaches the estimator the
wrong numbers.

## 1. Collect the facts

Start from the files the plugin kept for the ticket in `$HARNESS_HOME/tickets/<KEY>/`
(default `~/.claude/agent-harness/tickets/<KEY>/`): each agent's report (`<agent>.<NN>.md`,
listed in `index.jsonl`), its structured block (`analysis.json`, `estimate.json`,
`review.json`, `implementation.json`) and the quality gate result (`gates.json`). Reconstruct
from the conversation only what is missing there.

- **Estimate:** the estimator's hours, per part when it estimated bottom-up (`estimate.json`).
- **Actual time:** the person's own logged hours on the ticket from the tracker's worklogs,
  and the total if others logged too. Ask the person when the worklogs are not readable.
- **Size of the change:** for every repository the ticket touched, `git log --all
  --author=<person> --grep=<KEY> --numstat`: commits, files, lines added and removed, and how
  many of the added lines are tests.
- **Plan against reality:** the files in the analyst's plan (`analysis.json`) against the files
  that actually changed: what the plan missed, what it planned that was not needed.
- **Implementation:** the implementer's "Where the time went", failing test runs, steps the
  lead had to do by hand.
- **Review:** each finding in `review.json` and what the person did with it: accepted (fixed),
  rejected as noise or deferred.
- **Corrections:** anything the person said about an agent's output during the ticket.

Use numbers, not impressions. When a fact is missing, say so instead of guessing.

**Keep the numbers.** Once the facts are settled, record what the person did with each review
finding (`harness-eval review-finding <KEY> F1 … --disposition accepted|rejected|deferred`)
and freeze the ticket as an eval case (`harness-eval export-case <KEY> --source
measured|worklog --actual-part implementation=… verification=… release=… --repo <paths>`).

## 2. Have each agent propose its lessons

Run the agents in parallel, each with a short task: "Retro for <KEY>. Do not write to your
memory yet. From the following, propose the lines to add, each as `- <lesson> (source: <KEY>,
<today>[, expires YYYY-MM-DD])`, and the existing lines to change or delete." Use `retro` as
the source for a lesson that outgrows the ticket, and an expiry for one tied to a version, a
release or an incident. Send only what concerns that agent, and skip an agent with nothing new:

- **estimator:** a calibration row (repositories, files, lines with tests, the person's hours),
  the estimate against the actual time, and which part was off and why.
- **jira-analyst:** what the plan missed or overplanned, and traps found in the codebase.
- **implementer:** where the time went and the commands or conventions that would have saved it.
- **reviewer-qa:** the findings the person rejected and why, and the defects that got through
  the review.

## 3. Let the person decide

Show the person the proposed lines per agent, numbered, so that they can approve, edit or
reject each one briefly. Nothing goes into memory before that: a wrong lesson misleads every
later run, and a remembered rejection can hide the next real finding.

## 4. Write the approved lines

Send each agent with approved lines one task: "Write these approved lines to your memory
exactly as given, and make these approved changes and deletions: …". A hook denies a memory
line without a source and a date, and a file over its size cap; a compaction that the cap
asks for goes through the person like any other line.

## 5. Report

Show the person, briefly: the estimate against the actual time, the biggest lesson per agent,
and the lines each agent wrote. Every few retros, run `harness-eval memory lint` (README,
"Memory hygiene"): it lists undated and expired lessons, dead paths and files over the cap.
A retro stays local: never write it to the tracker unless the person asks for it.
