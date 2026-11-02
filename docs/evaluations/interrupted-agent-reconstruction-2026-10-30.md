# Interrupted-agent reconstruction — 2026-10-30

## Result

A Claude Code audit agent's session ended mid-task with three pull requests
open and no written handoff. A Codex successor, started with the interrupted
agent as its declared parent, located the audit's last work and its three open
PRs ([#3136](https://github.com/cirwel/unitares/pull/3136),
[#3137](https://github.com/cirwel/unitares/pull/3137),
[#3138](https://github.com/cirwel/unitares/pull/3138)) and finished them. All
three merged on 2026-10-30.

## How the successor found the work

1. **Lineage.** The successor called `start_session` with `parent_agent_id` set
   to the interrupted agent and `spawn_reason=explicit`. The server recorded the
   link as provisional lineage, and `check_working_state` confirmed the
   successor's binding.
2. **Timeline.** `GET /v1/timeline`, filtered to the predecessor over the
   preceding 12 hours, returned 12 check-ins, all `substrate_interpretation`
   rows. They show the predecessor was active and when; they carry no authored
   report text.
3. **Session pointer.** The predecessor's registered host-session pointer
   located its local Claude transcript. The transcript's last work narrative
   named delete-agent persistence as the cutoff.
4. **Git and PR records.** These recovered the three open PRs and the two
   uncommitted worktrees (an archival sweep and a deletion path).

## What each source supplied

UNITARES supplied the predecessor and session pointers and the record of the
predecessor's activity. The task itself came from the local transcript and the
Git and PR history. The recorded hook excerpts did not contain an authored task
handoff, so the case does not show UNITARES recovering task content on its own;
it shows the record placing the successor at the right transcript and the right
repository state. Writing a handoff finding at the end of a session is the step
that would put the task content in the record too.

Cross-agent export by `agent_id` was refused by the identity check; the
authenticated timeline was the supported way to read the predecessor's rows.

## Scope

One bounded operational case: no comparison against a baseline, and no
measurement of recovery speed or quality. The original implementation
provenance stays on the PRs, and the successor's delivery is recorded in their
handoff comments. The knowledge-graph note is `2026-10-30T06:49:09.377538+00:00`.
