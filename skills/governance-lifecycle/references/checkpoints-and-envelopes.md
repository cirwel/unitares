# Response Shapes, Checkpoints, and Predictions

Reference companion to `skills/governance-lifecycle/SKILL.md`. Load when
inspecting `response_shape`, `raw_governance_hint`, `checkpoint`,
`state_persistence`, `PREDICTION_REUSE_CONFLICT`, `calibration_excluded`, or
`review_suggested`.

## Response Shapes and `raw_governance_hint`

Read aliases, bounded `sync_state` modes, and the write aliases `store_finding`,
`update_finding`, and `record_result` omit the repeated canonical payload and
explain how to request it explicitly (`verbosity="full"` / alias `lite=false`
on `check_working_state`, `response_mode="full"` elsewhere, except as noted for
finding writes):

- **Plain fresh `start_session`**: returns `response_shape: "routine"` and omits
  the onboard record (pass `response_mode="full"` on the mint to keep it). On a
  thread earlier sessions occupied, `state_summary` carries
  `episode_fork_kind: "sibling_locus"` and the earlier node's `predecessor_uuid`,
  and `next_action` notes that co-location is not lineage. A named mint keeps
  its `resident_registration` verdict at top level in compact form (`status`,
  `on_roster`, and for `not_on_roster` / `caller_supplied_tags` a short `detail`
  explaining what that costs and how to add the name to the roster, restart, and
  mint fresh).
- **Routine `request_review`**: one whose review opened and waits on a known
  move returns `response_shape: "routine"` (`state_summary` keeps `session_id`,
  `phase`, and `whose_move`; `raw_governance_hint` names
  `dialectic(action="get", session_id=...)`, which returns current session state
  rather than the ack). A review ack carrying the review itself (verdict,
  recusal, facilitation request, or reviewer-host limit) keeps `raw_governance`
  with `response_shape: "full"` and `response_shape_reason`.
- **Clean `sync_state` proceed**: `response_shape: "routine"` keeps
  `action_summary.action`, `reason`, `risk_score`, and `margin` with its scope,
  dropping repeated approve/safe/healthy values. Anything unusual (a guide,
  pause, top-level `warnings`, resume miss, reactivated identity, declared
  lineage, spawn reason other than `new_session`, pruned sibling, or label
  rename) keeps the full shape; on `start_session` and `request_review`,
  `response_shape_reason` names the field that kept it.
- **`check_working_state(verbosity="standard")`**: middle tier returning basin
  and mode with meanings and guidance beside verdict, `risk_score`, and bare
  EISV values, without full diagnostics.
- **Write acks (`store_finding`, `update_finding`, `record_result`)**: keep the
  IDs you need next (`discovery_id`, `state_summary.outcome_id`). Finding writes
  carry `agent_uuid` and `written_as` (`agent_id`, `display_name`, assurance
  tier) — check it when bound without explicit `client_session_id`.
  `record_result` names its writer only when binding was not server-inferred.
  No read returns the omitted ack payload, so these acks do not set
  `raw_governance_available`:
  - For a prediction-bound `record_result`, repeating the identical call under
    the same identity (`client_session_id`) with `response_mode="full"` returns
    the stored outcome in full (`idempotent_replay: true`) without recording a
    second outcome while the binding is retained (a changed outcome is refused
    with `PREDICTION_REUSE_CONFLICT`). Without a `prediction_id`, a repeat
    records a second outcome, so `raw_governance_hint` names `response_mode="full"`
    on a later `record_result`.
  - For `store_finding` and `update_finding`, `raw_governance_hint` names
    `use_tool(tool_name="knowledge", arguments={"action": "details", "discovery_id": ...})`.
    Write-time warnings and a bounded `related_discoveries` snapshot stay in the
    ack itself (so `state_summary.related_to` does not repeat them). Never
    repeat `store_finding` just to see its payload — every call mints a new row.
- **`sync_state` memory suggestions**: `sync_state` does not retrieve shared
  memory unless `include_memory_suggestions=true` is explicit.

## Check-in Excerpts, Cold Start, and Typed Refusals

- `sync_state` retains a bounded excerpt of `response_text` (500 characters by
  default, cut at a word boundary, common credential shapes redacted). Put
  identifying pointers (PR numbers, paths) early, and never paste secrets
  (redaction is best-effort). The excerpt is a report, not a verified claim;
  durable claims still need `store_finding`.
- Cold-start action summaries carry a provisional headline. A `proceed` before
  the behavioral baseline forms is permission to continue under current policy,
  not a validated all-clear.
- **Typed identity refusals** deliberately do not use the normal envelope: they
  return `refused: true`, `status` (`identity_required` or
  `lineage_declaration_required`), `hint`, `next_step`, `safe_options`, `do_not`,
  and `rollout_flag`, with `success: true` and no `next_action`. The target
  handler did not run, though resolver-failure paths may have written
  identity-resolution bookkeeping. Follow `next_step` instead of retrying.

## Neutral Checkpoints and `state_persistence` Receipts

New servers return `checkpoint` when neutral acceptance is known:

- `status: accepted`, `checkpoint_id`, and per-identity `seq` acknowledge an
  immutable record committed before assessment; they are not a verdict or proof
  that later effects completed.
- For retry recovery, supply `idempotency_key` on the original call and reuse it
  with the same submitted content. `acceptance_retry_idempotent: true` covers
  that identity-scoped acceptance: a retry returns the original receipt without
  repeating assessment. Changed content conflicts; the same key under a new
  identity is a different scope. Current identity, lifecycle, and cooldown gates
  still apply to retries. A receipt-only replay carries no fresh policy action
  or prediction — do not infer operational permission from its `success: true`.
- Acceptance `status: unconfirmed` may follow a committed record whose
  acknowledgement was lost; without an explicit key, another call can create a
  new record.
- `checkpoint.assessment.status` reports execution: `completed` means execution
  returned (not atomic durability of every effect); `failed` may have partial
  effects; a crash can leave `started` ambiguous. Keyed retries never restart
  these deliveries, including ones that never started.

On completed check-ins that reach the state writer, `state_persistence`
describes the assessment-row acknowledgement:

- `status: recorded` + positive `state_id` confirms the writer acknowledged that
  row (not every other check-in effect).
- `status: unconfirmed` + `state_id: null` means no valid acknowledgement
  arrived; the row may still exist if commit completed before disconnect.
- Read `state_persistence` separately from top-level `success`. Its legacy
  `checkin_retry_idempotent: false` reflects that the assessment writer does not
  deduplicate itself; use the `checkpoint` `idempotency_key` contract above
  instead of repeating full check-ins. Early refusals and older servers may omit
  this receipt; absence does not prove nothing was written.

## Prediction Binding and Review Nudges

- A genuine `confidence` on `sync_state` may mint a concrete `prediction_id`.
  Pass that exact ID to `record_result(..., prediction_id="...")`; otherwise the
  outcome may grade an unrelated fallback prediction.
- The advertised ID is the check-in's own mint: evidence rows passed in
  `recent_tool_results` bind to their own prediction IDs, which the reply never
  advertises (preventing `PREDICTION_REUSE_CONFLICT` from reusing an already
  consumed evidence-row ID).
- `record_result`'s `state_summary` reports `prediction_binding` and
  `prediction_source` (which prediction was graded), and `calibration_excluded: true`
  when confidence was scraped rather than bound (meaning the row does not train
  calibration).
- When `UNITARES_REVIEW_NUDGE` is enabled, a warmed session may receive a
  once-per-session `review_suggested` nudge for low confidence, high complexity,
  or a guide verdict, pointing to `request_review` (on-record review) or
  `consult(purpose="critique")` (advisory design critique). It is optional
  guidance, not a forced review.

## Host Automation Differences

Hosts differ in what they automate: some write a per-turn
`substrate_interpretation` for you, others write almost nothing. Do not infer
from a quiet history that you are being watched less, or from a busy one that
you have already reported.
