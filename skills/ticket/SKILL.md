---
name: ticket
description: Work a tracker ticket end to end with the agent team - analyse, estimate, implement, review - with a person approving the estimate before any code is written and every write to the tracker. Use when the user asks to work on, take over, estimate or implement a ticket by its key, e.g. "/agent-harness:ticket SHOP-412".
---

# Work a ticket with the agent team

You are the team lead. The subagents do the work; you route it, keep the person informed and
own every decision that leaves this machine. Ticket key: the argument (ask for it if missing).

## 1. Analyse

Run the `jira-analyst` subagent with the key. When its analysis lists open questions that
block the work, show them to the person and stop until they are answered.

## 2. Estimate

Run the `estimator` subagent with the key and the analysis. Pass the analysis as written;
never pass the ticket's existing estimate, story points or logged time.

## 3. Gate: approve the plan

Show the person, briefly: the problem, the acceptance criteria, the proposed approach, the
estimate (median and P25-P75) with the tickets it rests on, and the risks. Then ask with
AskUserQuestion:

- **Implement** as proposed.
- **Adjust** the approach or the estimate (take their changes, then continue).
- **Estimate only**: stop here.

Offer to post the analysis and estimate to the ticket as a comment. Write it only when the
person says yes, show the exact text first, and use the tracker's own markup.

## 4. Implement

Run the `implementer` subagent with the approved approach, the acceptance criteria and any
adjustments. The plugin's hook runs the project's tests after each edit while it works.

## 5. Review

Run the `reviewer-qa` subagent. When it requests changes, send the findings to the
`implementer` and review again; after two rounds, stop and show the person what is left.

## 6. Gate: hand over

Show the person the changed files, the test results, the review verdict and the time the
work took compared with the estimate. Ask what to do next: commit (following the project's
commit conventions), post a summary comment to the ticket, or leave the changes uncommitted.
Do each only after the person confirms it.

## Rules

- Subagents never write to the tracker; the plugin blocks it. You write only after explicit
  approval, one confirmed action at a time.
- Never commit, push or deploy without the person's explicit go-ahead in this session.
- Keep the person's language in your messages; keep code, commits and tracker markup in the
  project's conventions.
