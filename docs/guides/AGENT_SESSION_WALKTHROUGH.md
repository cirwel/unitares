# Agent session walkthrough

One task, two processes, every call an agent makes, with what the server
returned. Process A finds the cause of a flaky test, records it, checks in,
grades the check-in against the test result, closes the finding and exits
cleanly. Process B starts
later, finds A's record before redoing the work, and declares A as its
predecessor.

The task is illustrative. The responses are not: they were captured on
2026-10-08 (04:32 UTC) from a fresh Docker quickstart install at commit
`df13c7166`, over MCP Streamable HTTP, with no model configured. Responses are
abridged to the fields the step is about; field names and values are verbatim.
Identifiers will differ on every install.

The rules behind each step are in
[Integrating agents](../manual/04-integrating-agents.md) and the
[identity ontology](../ontology/identity.md). Exact schemas come from
`list_tools()` and `describe_tool()`.

## Process A

### 1. Search before doing anything

Reading needs no identity, so the first call can come before
`start_session`.

```json
search_shared_memory {"query": "flaky upload retry test", "limit": 5}
```

```json
{
 "success": true,
 "next_action": "No prior discoveries matched. Broaden terms or search tags before recording a new finding.",
 "state_summary": {"count": 0, "search_mode_used": "fts", "result_tier": "lean_digest", "results_shown_in_digest": 0}
}
```

### 2. Start a fresh identity

```json
start_session {"force_new": true, "name": "upload-fixer", "model_type": "claude"}
```

```json
{
 "client_session_id": "agent-2dca3de6-b0d-ulgnzjn7l3qkmn5zalhl",
 "agent_uuid": "2dca3de6-b0d8-4c5b-aa83-916bc1f62349",
 "display_name": "upload-fixer",
 "is_new": true,
 "identity_resolution_outcome": "minted_force_new",
 "label_is": "social_or_cosmetic",
 "identity_assurance": {"tier": "weak", "session_source": "ip_ua_fingerprint", "caller_proven": false, "baseline": "fresh_identity"},
 "next_action": "Save agent_uuid and client_session_id, then check in with sync_state(response_text='...', complexity=0.5, client_session_id=...) as you work."
}
```

The identity is `agent_uuid`, not `name`: `label_is` says the name is
cosmetic. Pass `client_session_id` on every later call from this process.

`tier: weak` describes the minting call, which carried no session signal of
its own. It does not mean later calls are unthreaded; step 3 checks that.

### 3. Confirm the calls are threaded

```json
check_working_state {"client_session_id": "agent-2dca3de6-b0d-ulgnzjn7l3qkmn5zalhl", "lite": true}
```

```json
{
 "agent_uuid": "2dca3de6-b0d8-4c5b-aa83-916bc1f62349",
 "action_summary": {"action": "uninitialized", "verdict": "uninitialized", "verdict_confidence": "provisional", "evidence_basis": "ode_fallback"},
 "next_action": {"tool": "sync_state", "example": "sync_state(response_text='Completed a meaningful step', complexity=0.3)", "note": "check_working_state is read-only; it does not initialize state."}
}
```

The returned `agent_uuid` matches step 2, so `client_session_id` is binding
this process's calls to its own identity.

### 4. Record what a later process would search for

```json
store_finding {
 "client_session_id": "agent-2dca3de6-b0d-ulgnzjn7l3qkmn5zalhl",
 "discovery_type": "bug_found",
 "summary": "test_upload_retry flakes because the fake clock fixture is module-scoped and leaks time between tests",
 "content": "Under random test order the retry backoff sees time already advanced by an earlier test and gives up before the third attempt. Making the fixture function-scoped removes the failure in 50 consecutive shuffled runs.",
 "tags": ["flaky-test", "uploads"]
}
```

```json
{
 "discovery_id": "2026-10-08T04:32:44.421205+00:00",
 "next_action": "When this is addressed, close the loop: use_tool(tool_name='update_finding', arguments={\"discovery_id\": \"2026-10-08T04:32:44.421205+00:00\", \"status\": \"resolved\"})",
 "state_summary": {"type": "bug_found", "status": "open", "message": "Discovery stored for agent 'upload-fixer'"}
}
```

### 5. Check in after meaningful work

```json
sync_state {
 "client_session_id": "agent-2dca3de6-b0d-ulgnzjn7l3qkmn5zalhl",
 "response_text": "Found why test_upload_retry flakes (shared fake clock); fixture made function-scoped, verifying with shuffled runs.",
 "complexity": 0.4,
 "confidence": 0.7,
 "task_type": "bugfix"
}
```

```json
{
 "action_summary": {
  "headline": "Provisional: proceed; behavioral evidence is still forming.",
  "action": "proceed",
  "reason": "Low risk (24.9%) - healthy operating range",
  "verdict_confidence": "provisional",
  "evidence_basis": "ode_fallback"
 },
 "next_action": "Keep working; sync_state after your next substantial step. When an outcome lands, pass this prediction_id to record_result so it grades this check-in.",
 "prediction_id": "e269304b-65a2-4a30-87ba-85668eb3022c",
 "verdict_caveat": "Verdict is provisional: the behavioral baseline is not warm. 'safe'/'proceed' means no trouble detected under the cold-start prior, not a validated all-clear. Evidence basis: ode_fallback."
}
```

`action` is the policy action: `proceed`, `guide` or `pause`. On a fresh
identity it is provisional, and the response says so. Keep the
`prediction_id`.

### 6. Grade the check-in against what happened

```json
record_result {
 "client_session_id": "agent-2dca3de6-b0d-ulgnzjn7l3qkmn5zalhl",
 "outcome_type": "test_passed",
 "detail": {"test_name": "test_upload_retry", "runs": 50, "order": "shuffled"},
 "prediction_id": "e269304b-65a2-4a30-87ba-85668eb3022c"
}
```

```json
{
 "next_action": "Outcome recorded - continue, or sync_state to fold it into your working state.",
 "state_summary": {
  "outcome_type": "test_passed",
  "outcome_score": 1.0,
  "corroboration_grade": "self_report_with_refs",
  "evidence_weight": 0.35,
  "corroboration_reasons": ["agent report includes references but no verified evidence", "verification_source is agent_reported_tool_result"],
  "prediction_binding": "registry",
  "calibration_excluded": false
 }
}
```

The outcome is bound to the check-in from step 5 and counts toward
calibration. It is still graded as a self-report, because the agent reported
the test result itself.

Without `prediction_id`, the same call is recorded but excluded from
calibration. On an earlier capture the server answered: "Outcome recorded, but
stamped calibration_excluded (binding: prev_confidence_fallback), so it does
not train calibration. Pass the prediction_id from the sync_state you are
grading to bind the outcome to it."

### 7. Close the finding

The fix is verified, so the finding from step 4 should not stay open for the
next process to rediscover.

```json
use_tool {
 "client_session_id": "agent-2dca3de6-b0d-ulgnzjn7l3qkmn5zalhl",
 "tool_name": "update_finding",
 "arguments": {
  "client_session_id": "agent-2dca3de6-b0d-ulgnzjn7l3qkmn5zalhl",
  "discovery_id": "2026-10-08T04:32:44.421205+00:00",
  "status": "resolved",
  "closure_class": "fix_verified",
  "closure_evidence": {
   "deployed": "fake clock fixture changed from module scope to function scope",
   "observed": "test_upload_retry passed 50 consecutive runs in shuffled order"
  }
 }
}
```

```json
{
 "message": "Discovery '2026-10-08T04:32:44.421205+00:00' status updated to 'resolved'",
 "closure_class": "fix_verified",
 "state_summary": {"type": "bug_found", "status": "resolved", "resolved_at": "2026-10-08T04:32:44.562641+00:00"}
}
```

`closure_class` records what the closure rests on. A bare `status: resolved`
is accepted, but on an earlier capture the server warned: "This closure
declares no standard and reads as unclassified: a later reader cannot tell it
from a closure resting on a deployed fix whose effect was observed."
`fix_verified` needs `closure_evidence` with what was deployed and what was
observed.

### 8. Exit cleanly

```json
use_tool {
 "client_session_id": "agent-2dca3de6-b0d-ulgnzjn7l3qkmn5zalhl",
 "tool_name": "agent",
 "arguments": {"action": "release_presence", "client_session_id": "agent-2dca3de6-b0d-ulgnzjn7l3qkmn5zalhl"}
}
```

```json
{"action": "release_presence", "released": true, "reason": "released", "bindings_retired": 0}
```

This tells the server the process has ended. Skip it and the process still
reads as live for up to ten minutes, and a successor that names it as parent
in that window is refused. On an earlier capture without this step, process B
got `"lineage_state": "rejected_coincidental"` and was told: "A prior node in
this thread was detected, but thread co-location does not establish lineage.
Pass parent_agent_id only if you are deliberately continuing a process that
has ended." A crash never reaches this call; the ten-minute expiry is the
backstop.

## Process B

### 9. Search first, again

```json
search_shared_memory {"query": "flaky upload retry test", "limit": 5}
```

```json
{
 "next_action": "1 prior discoveries matched - read before redoing work. Full context: knowledge(action='details', discovery_id=...). Record new findings: store_finding(summary='...'). Revise one: use_tool(tool_name='update_finding', arguments={\"discovery_id\": \"...\", \"summary\": \"...\"}).",
 "memory_suggestions": [{
  "type": "bug_found",
  "status": "resolved",
  "closure_class": "fix_verified",
  "discovery_id": "2026-10-08T04:32:44.421205+00:00",
  "summary": "test_upload_retry flakes because the fake clock fixture is module-scoped and leaks time between tests",
  "by": "upload-fixer",
  "agent_id": "2dca3de6-b0d8-4c5b-aa83-916bc1f62349"
 }]
}
```

B finds A's record before it has an identity. The record names the process
that wrote it and says the fix was verified, so B does not redo the work.

### 10. Start fresh and declare the handoff

```json
start_session {
 "force_new": true,
 "name": "upload-fixer-2",
 "model_type": "claude",
 "parent_agent_id": "2dca3de6-b0d8-4c5b-aa83-916bc1f62349",
 "spawn_reason": "explicit"
}
```

```json
{
 "client_session_id": "agent-f70d8be8-366-bbftq35ql5vjpwxlcifx",
 "agent_uuid": "f70d8be8-3669-482b-936d-66a1b150eccf",
 "identity_resolution_outcome": "minted_force_new",
 "next_action": "Save agent_uuid and client_session_id, then check in with sync_state(response_text='...', complexity=0.5, client_session_id=...) as you work. Declared lineage for this fork is already recorded; do not redeclare it on the current session.",
 "state_summary": {"lineage_state": "provisional", "episode_fork_kind": "identity_lineage", "predecessor_uuid": "2dca3de6-b0d8-4c5b-aa83-916bc1f62349"}
}
```

B is a new identity with its own `agent_uuid`. It does not resume A's. The
lineage records that B inherited A's work, which is a different claim from B
being A. Declare a parent only for a real handoff like this one, or for a
dispatched subagent (`spawn_reason="subagent"`), never because another process
shares the workspace.

## What not to do

Each of these is a real failure mode, and the identity rules exist because of
them:

- Calling `start_session()` or `identity()` with no arguments to find out who
  you are. A bare call can let weak session evidence resume an unrelated
  identity. Use `force_new=true`.
- Resuming from a bare `agent_uuid` or a display name. A UUID is a server
  record, not proof that the current process owns it.
- Passing `continuity_token` on every call. It is an advanced same-process
  rebind proof, not part of the normal workflow.
- Writing a finding without searching first.
