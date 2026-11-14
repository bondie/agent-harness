---
name: work
description: Mark the tracker ticket this session works on, so that `harness-eval active-time` counts the session's busy time for it - "/agent-harness:work SHOP-412" to start, "/agent-harness:work stop" to end. Use only when the person types it; the ticket playbook marks its ticket on its own.
---

# Mark the ticket this session works on

The plugin's hook has already recorded the marker when this skill loads: from now on, until
the next marker in this session, the time the session and its subagents are busy counts for
the ticket in the argument; `stop` ends it.

Reply with one line: the ticket now counted (or that counting stopped). Do nothing else, and
do not repeat the marker: a second call would only log it again.

The playbook (`/agent-harness:ticket <KEY>`) sets the marker by itself. This skill is for
work outside it: a helpdesk thread, a quick fix, the deploy of a ticket in a new session, or
returning to a ticket after a detour in the same session.
