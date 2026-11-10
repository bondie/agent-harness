---
name: ticket
description: Work a tracker ticket end to end with the agent team - analyse, estimate, implement, review - with a person approving the estimate before any code is written and every write to the tracker. Use when the user asks to work on, take over, estimate or implement a ticket by its key, e.g. "/agent-harness:ticket SHOP-412".
---

# Work a ticket with the agent team

You are the team lead. The subagents do the work; you route it, keep the person informed and
own every decision that leaves this machine. Ticket key: the argument (ask for it if missing).

Start every subagent task with the key and a colon, `<KEY>: …`. The plugin files each run's
report under that key; a task without it lands in an `adhoc-…` directory that the retro
and the evals cannot find.

## 1. Analyse

Run the `jira-analyst` subagent with the key ("SHOP-412: analyse the ticket"). When its
analysis lists open questions that
block the work, show them to the person and stop until they are answered.

## 2. Estimate

Run the `estimator` subagent with the key and the analysis ("SHOP-412: estimate this plan
for <person>. …"). Pass the analysis as written,
including its implementation plan; never pass the ticket's existing estimate, story points
or logged time.

Tell the estimator who will do the work. With an implementation plan it estimates bottom-up
from that person's calibration, kept in its memory. When it reports that the calibration is
missing or out of date, collect it and run it again: the person's own logged time on their
recent finished tickets (the tracker's worklogs) next to the size of their commits for each
ticket (`git log --author=<person> --numstat`, grouped by the ticket key in the message).
Ten to twenty tickets of different sizes are enough.

When the person rejects an estimate, give the estimator what they said and ask it to propose
the correction as one memory line, `- <correction and revised estimate> (source: <KEY>,
<today>)`, without writing it. Show the line to the person; send it back to be written exactly
as given only once they approve or edit it, as in the retro.

When the precedent index separates platforms (`list_sources` shows a `platform` per source),
tell the estimator the ticket's platform, as the analysis names it. Tickets of another
platform distort the estimate: the same kind of work can take several times longer on a
legacy product.

When the project's instructions (CLAUDE.md) name a date since which the team works
differently, for example implementing with AI agents, tell the estimator the date and ask
it to call `effort_reference` with `since` set to it. Older tickets then overstate the
effort: in the backtest, a reference class limited to tickets after such a change cut the
mean absolute error by almost 30%.

## 3. Gate: approve the plan

Show the person, briefly: the problem, the acceptance criteria, the proposed approach, the
estimate (one number in hours) with the evidence it rests on, and the risks. Then ask with
AskUserQuestion:

- **Implement** as proposed.
- **Adjust** the approach or the estimate (take their changes, then continue).
- **Estimate only**: stop here.

Offer to post the analysis and estimate to the ticket as a comment. Write it only when the
person says yes, show the exact text first, and use the tracker's own markup.

## 4. Implement

Run the `implementer` subagent with the approved approach, the acceptance criteria and any
adjustments ("SHOP-412: implement the approved change. …"). The plugin's hook runs the
project's tests after each edit while it works, and the repository's quality gate
(`.claude/agent-harness.json`) when it finishes; read `gates.json` before you trust that the
checks passed. The plugin keeps every agent's report and structured block in
`~/.claude/agent-harness/tickets/<KEY>/` (under `$HARNESS_HOME` when set) for the retro.

## 5. Review

Run the `reviewer-qa` subagent ("SHOP-412: review the change. …"). When it requests
changes, send the findings to the `implementer` ("SHOP-412: fix these review findings. …")
and review again; after two rounds, stop and show the person what is left. Each round keeps
its own review block for the retro.

## 6. Gate: hand over

Show the person the changed files, the test results, the review verdict and the time the
work took compared with the estimate. Ask what to do next: commit (following the project's
commit conventions), post a summary comment to the ticket, or leave the changes uncommitted.
Do each only after the person confirms it.

When the project has its own skills for releasing and deploying, offer them here; they run only
with the person's explicit go-ahead for that release or deploy.

## 7. Retro

When the work is done and its time is logged, run the `retro` skill with the key, now or in
a later session. It compares the estimate and the plan with what happened, has each agent
propose its lessons, and writes to memory only the ones the person approves. Offer it at the
hand-over; a team that never runs it does not get better.

## Rules

- Subagents never write to the tracker; the plugin blocks it. You write only after explicit
  approval, one confirmed action at a time.
- Never commit, push or deploy without the person's explicit go-ahead in this session.
- Keep the person's language in your messages; keep code, commits and tracker markup in the
  project's conventions.
