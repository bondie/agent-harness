# Shared team memory (design note)

Status: a proposal for discussion, nothing here is implemented.

## Today

Every agent has `memory: user`, so its memory lives in
`~/.claude/agent-memory/agent-harness-<agent>/` on one person's machine. The memory guard
holds every lesson to the line format (`- <lesson> (source: <KEY|MR|commit|retro>,
YYYY-MM-DD[, expires YYYY-MM-DD])`) and a size cap, `harness-eval memory lint|prune` keeps
it clean, and the retro writes only the lines the person approved.

What that leaves out:

- A trap one person's implementer learned in a codebase (a command that needs a running
  container, a cache to clear) is unknown to a colleague's implementer in the same codebase.
- A review finding the team rejected is remembered only by the reviewer of the person who
  rejected it, so the next person's reviewer raises it again.
- No one but the person who ran the retro sees what the agents learned.

Some memory should stay personal: the estimator's calibration is one person's logged hours
against their diffs, and corrections to that person's estimates are theirs.

## Options

### 1. A project layer in the repository

Claude Code's `project` memory scope keeps an agent's memory in `.claude/agent-memory/<agent>/`
inside the repository, shared through version control (`local` scope:
`.claude/agent-memory-local/`, not checked in).

- Writes go through a merge request, not straight from the agent: the retro turns the
  approved lines into a branch, and the team reviews them like code.
- The `memory` field takes one scope per agent, so an agent cannot have both its personal and
  the project layer through it. Either the agent keeps `memory: user` and its prompt tells it
  to read `.claude/agent-memory/<agent>/MEMORY.md` from the repository, or the project facts
  move to `CLAUDE.md` and the codebase's review skill, which every session loads already.
- The memory guard treats `agent-memory/<agent>/` under any root, the repository's included,
  as the agent's own and lets it write there; with writes through merge requests, it would
  have to deny agents direct writes to the project layer.

### 2. Relevance filtering per task

Claude Code loads the first 200 lines or 25 KB of `MEMORY.md` into every run, whatever the
task. The compound-engineering plugin instead keeps its learnings in `docs/solutions/` with
classified front matter, and its planning and review steps search them for the ones relevant
to the change at hand. For agent-harness that would mean tagging each lesson with the
repository and the paths or area it concerns, and loading into a run only the lessons that
match the analyst's plan. It pays off once a shared layer outgrows the cap; before that, a
short `MEMORY.md` index with topic files read on demand is enough.

### 3. Approval through the retro

The retro already shows each proposed line to the person, who approves, edits or rejects it.
For a shared layer, the approved lines become a merge request instead of a direct write, and
a second person approves them there. The `source:` in each line points at the ticket or merge
request it came from, so a reviewer can check the fact behind it.

## Risks

- **Secrets and client names in a shared repository.** Lessons are written from real tickets
  and logs: host names, a token pasted into a command, a customer's name, ticket text. In a
  repository more people or vendors can read than the person's home directory, that is a leak.
  The merge request needs the repository's secret scan, a check against a list of client
  names, and a rule that lessons describe the codebase, never the customer.
- **Personal data.** Calibration rows are one named person's logged hours. They stay in the
  personal layer; sharing them turns an estimation aid into a performance record.
- **Conflicting lessons.** Two people learn opposite things ("run the specs in the container"
  against "run them on the host since the upgrade"), or a personal lesson contradicts a
  project one. The date in each line lets the newer one win, an expiry retires lessons tied to
  a version, and the merge request is where a person resolves the conflict; lint could flag
  lessons on the same path with different advice.
- **Poisoned memory.** A merged lesson is an instruction to every agent that loads it. It
  needs the same review as code; an agent must never merge to the project layer itself.
- **Growth.** A team adds lessons faster than one person. The size cap and `memory prune`
  apply per file, and the retro's "change or delete" proposals have to keep pace.

## Recommendation

Keep two layers. The personal layer stays as it is, for the estimator's calibration and the
corrections to one person's estimates. Add a project layer for codebase facts only:
commands, traps and conventions (implementer, analyst) and rejected review findings
(reviewer). Fill it only through merge requests the retro prepares, and start without
relevance filtering, with a `MEMORY.md` index under the cap. Pilot it on one repository with
the implementer and the reviewer, and measure before extending it: whether the reviewer's
acceptance rate rises (`harness-eval metrics`) and whether the implementers' "Where the time
went" stops repeating the same traps.

## Open questions for the team

1. Which agents get a project layer, and do the analyst's codebase notes belong there or in
   `CLAUDE.md`?
2. Who approves a memory merge request: the author of the change, a code owner, anyone?
3. In a repository shared by several clients' projects, where do client-specific lessons go?
4. When a personal lesson and a project lesson disagree, which one wins in a run?
5. Does `harness-eval memory lint` run in CI on the project layer, and does a finding fail it?
6. Is the `local` scope (`.claude/agent-memory-local/`) useful for anything the personal
   layer does not already cover?
