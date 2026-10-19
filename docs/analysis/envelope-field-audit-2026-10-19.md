# Experience envelope field audit — 2026-10-19

Bounded audit for [#2125](https://github.com/cirwel/unitares/issues/2125):
which fields the canonical handlers mint does `build_experience_envelope`
(`src/mcp_handlers/middleware/envelope_step.py`) drop, and which of those
drops matter.

The envelope rebuilds each friendly response from scratch instead of
filtering the canonical payload, so a minted field can disappear without a
failing test. That happened with `prediction_id` (#2123, fixed in #2124). An
earlier static audit (the comment on #2125, read at `238ce217`) listed the
gaps per alias. This document re-reads them against `origin/master` at
`75d3a9c6` and classifies each one. Much changed in between: #2439 bounded
the three write acknowledgements, #2437 bounded routine `start_session` and
`sync_state`, and the metrics read gained `_metrics_next_action` and
`_metrics_identity_assurance`.

This is a source reading plus unit tests on the real producers where the
suite has one (onboard handler, metrics read, check-in formatter, lean search
projection). No live server was called.

## Classes

- **R (required preserved).** The agent cannot finish a documented loop
  without it. Two kinds:
  - **R-id:** a value the agent must pass back on a later call.
  - **R-signal:** a signal the producer documents as for the caller, which
    no later read returns, so a dropped copy is gone for good.
- **I (intentional omission).** Redundant with a lifted field, an echo of
  the caller's own request, budget-driven with a precedent, or available
  under `raw_governance` whenever the envelope keeps it.
- **O (open).** A judgement this audit does not make. Listed for a
  follow-up, not fixed here.
- **N (not a drop).** The field is never minted on this path, or the
  response never reaches the envelope (for example `success: false`).

## Scope

Eight aliases carry `experience=True` in `tool_stability.py` (enforced by
`test_every_experience_alias_declares_its_required_fields`). After this
change every alias's default shape omits `raw_governance`:

| Alias | Canonical | Default omits raw? | Kept when |
|---|---|---|---|
| `start_session` | `onboard` | routine mint | non-routine mint, or `response_mode` full/standard/verbose |
| `sync_state` | `process_agent_update` | bounded modes (`_mode` set) | `response_mode='full'`, or auto with no `_mode` |
| `check_working_state` | `get_governance_metrics` | `verbosity='minimal'` | standard or full |
| `search_shared_memory` | `knowledge(search)` | lean | `response_mode='full'` |
| `store_finding` / `update_finding` | `knowledge(store/update)` | when a `discovery_id` is liftable | no liftable id, or an explicit other action (#2457) |
| `record_result` | `outcome_event` | when an `outcome_id` is liftable | `response_mode='full'` / `include_semantics` |
| `request_review` | `dialectic(request)` | routine ack (new here, #2443) | the ack carries the review itself |

Refusals and `success: false` payloads bypass the envelope.

## Summary of required fields

These are pinned in `tests/test_envelope_required_fields.py`. Each one is
run through the alias's default shape and asserted reachable outside
`raw_governance` with the minted value.

| Alias | Field | Kind | Envelope path | State before this change |
|---|---|---|---|---|
| `start_session` | `uuid` | R-id | `agent_uuid` | preserved |
| `start_session` | `client_session_id` | R-id | `client_session_id` | preserved |
| `start_session` | `continuity_token` | R-id | `rebind.continuity_token` | preserved (#2437) |
| `sync_state` | `prediction_id` | R-id | `prediction_id` | preserved (#2124) |
| `sync_state` | `warnings` | R-signal | `warnings` | **dropped in every bounded mode; fixed** |
| `check_working_state` | `agent_uuid` | R-id | `agent_uuid` | preserved |
| `search_shared_memory` | digest `discovery_id`s | R-id | `memory_suggestions[*].discovery_id` | preserved |
| `search_shared_memory` | `limit_clamped_from` | R-signal | `state_summary.limit_clamped_from` | **dropped by the envelope; fixed** |
| `search_shared_memory` | `imported_omitted`, `imported_omitted_hint` | R-signal | `state_summary.*` | **dropped by the lean projection, so the envelope's lift was dead; fixed** |
| `store_finding` / `update_finding` | `discovery_id` | R-id | `discovery_id` | preserved (#2439) |
| `record_result` | `outcome_id` | R-id | `state_summary.outcome_id` | preserved (#2439) |
| `request_review` | `session_id` | R-id | `state_summary.session_id` | preserved (and now the only copy on a routine ack) |

Why the three R-signal fixes count as required:

- **`sync_state` `warnings`.** `update_response_service.build_process_update_response_data`
  mints them, and the `process_agent_update` description says to inspect
  them "to detect silent calibration-loss". Examples: "confidence dampened"
  on a weak binding, and a failed evidence write. A warning is one of the
  formatter's own triggers for expanding auto to mirror
  (`_auto_response_mode`), and every bounded formatter passes `warnings`
  through. The envelope then dropped them. Its hint names
  `response_mode='full'` on the *next* check-in, which cannot return this
  check-in's warnings. Precedent: #2439 lifted write-time warnings into the
  write acks for the same reason. A check-in that carries warnings is no
  longer trimmed as a routine proceed.
- **`limit_clamped_from`.** The search handler keeps it in its own lean
  projection, with the comment "a clamped caller must be able to tell
  truncation from 'that was all'". The envelope's state lift did not name
  it, and `count` alone cannot tell the two apart.
- **`imported_omitted` / `imported_omitted_hint`.** A queryless listing that
  left unpromoted imported memory out says so, and says how to read that
  lane. The envelope already lifted both keys, but the lean projection
  (`knowledge/handlers.py` `_lean_search_payload`) dropped them first. On the
  friendly default the remainder therefore read as the whole set.

## Per alias

### start_session -> onboard

| Field | Treatment | Class |
|---|---|---|
| `uuid`, `client_session_id`, `continuity_token` | lifted (`agent_uuid`, top level, `rebind`) | R-id, preserved |
| `agent_id`, `display_name`, `is_new`, `identity_resolution_outcome`, `session_resolution_source` | lifted (`_ONBOARD_LIFT_KEYS`) | I (#2437) |
| `identity_assurance` | compact (`tier`, `session_source`, `caller_proven`, `baseline`) unless abnormal | I (#2437) |
| `lineage_state`, `onboard_origin*`, `thread_context.predecessor.uuid`, `episode_fork_kind` | `state_summary` | I |
| `next_step` | replaced by a fixed `next_action` that says the same thing | I |
| `session_key` | lifted but never minted on onboard | N (dead lift) |
| `welcome`, `thread_context`, `provisional_lineage`, verbose fields | only in `raw_governance`, which a non-routine mint keeps | I (#2437 routine-mint rule) |

### sync_state -> process_agent_update

| Field | Treatment | Class |
|---|---|---|
| `prediction_id` | top level | R-id, preserved |
| `warnings` | **now lifted top level when bounded** | R-signal, fixed |
| `decision.action/margin/nearest_edge/margin_scope/unmeasurable_edges`, `sub_action`, `status`, coherence, risk | `state_summary` / `action_summary` | I |
| `decision.reason`, verdict, `policy_evaluation`, applied `enforcement` | folded into `action_summary` (`_decision_action`, `_action_summary`) | I |
| `review_suggested` | prose in `next_action` | I (#1685) |
| `outcomes_in_e`, `reflection` | lifted | I |
| `relevant_discoveries` / `relevant_prior_work` | only with `include_memory_suggestions=true` | I |
| `decision.require_human` | formatters copy it, but no live producer sets it | N |
| `mirror[]` (mirror mode) | dropped | O: its actionable lines are lifted elsewhere (verdict, outcomes, review nudge, margin). Complexity-calibration, pace and fleet-health lines have no lift |
| `identity_assurance`, `identity_notifications`, `evidence_status`, `knowledge_surfacing_degraded` | dropped | O: a weak binding's effect on the check-in reaches the agent through `warnings` now. `identity()` reads the binding |
| E/I/S/V, phi, lambda1, `trust_tier`, `summary`, `_tip`, `eisv_contract`, `thread_context` | dropped | I (#2332 budget). `check_working_state` returns E/I/S/V |

### check_working_state -> get_governance_metrics

| Field | Treatment | Class |
|---|---|---|
| `agent_uuid` | top level | R-id, preserved |
| verdict, status, `primary_eisv_source`, E/I/S/V, coherence badge, risk | `state_summary` | I |
| next step | `_metrics_next_action`, every tier | I (fixed since `238ce217`) |
| inferred-binding assurance | `_metrics_identity_assurance` | I (fixed since `238ce217`) |
| `write_gate` | `action_summary` | I |
| `mode`, `basin`, guidance meanings | standard / full | I (the alias text says so) |
| `thresholds` | dropped | O: the only place the cut-offs are quoted |
| `agent_id`, `display_name`, `public_handle`, `purpose`, `summary`, `related_tools`, `_note` | dropped | I |

### search_shared_memory -> knowledge(search)

| Field | Treatment | Class |
|---|---|---|
| digest `discovery_id`s | `memory_suggestions`, kept through budget truncation | R-id, preserved |
| `limit_clamped_from` | **now `state_summary`** | R-signal, fixed |
| `imported_omitted`, `imported_omitted_hint` | **now kept by the lean projection, lifted to `state_summary`** | R-signal, fixed |
| `count`, `search_mode_used`, `low_confidence`, `confidence_note`, `search_degraded*`, ordering and window echoes | lifted | I |
| further results | `more_results` rows | I |
| `_more_available` | dropped | I: an unclamped "may be limited to N" hint, which the caller can derive from `count` and `more_results`. The clamped case is covered by `limit_clamped_from` |
| `search_mode_requested`, `operator_used`, `tag_filter_dropped`, `total_count` | lifted but removed one layer down by the lean projection | O (dead lifts, as in #2125's comment) |
| `caller` (the reader's own uuid and assurance), `query`, `message` | dropped | I |

### store_finding / update_finding -> knowledge(store / update)

Every gap in the earlier audit was closed by #2439. `discovery_id`, the
write-time warnings, `closure_class` with its note, supersession outcomes and
a bounded `related_discoveries` snapshot are all lifted. `written_as` comes
from the signature. Nothing is open.

### record_result -> outcome_event

`outcome_id` (R-id) and the binding disclosure are lifted, and so are
`is_bad`, `idempotent_replay` and `unverified_fields` (#2439). `recorded_at`
is lifted but never minted (N, dead lift). `claimed_fields` and
`verified_fields` are dropped (I): the actionable part is `unverified_fields`.

### request_review -> dialectic(request)

| Field | Treatment | Class |
|---|---|---|
| `session_id` | `state_summary` | R-id, preserved |
| `phase`, `whose_move`, `one_call_review`, `thesis_source`, `review_verdict`, `reviewer_agent_id` | `state_summary` | I |
| `orchestrated_review`, `reviewer_dispatch` | **now `state_summary`** (the dialectic canary records `orchestrated_review`) | I, lifted with the #2443 change |
| `next_call` / `next_step` | `next_action` | I |
| `thesis_recorded: false`, `session_created` | only on `success: false`, which passes through whole | N (the earlier audit's "b (low)" is withdrawn) |
| antithesis, synthesis, recommendation, recusal, `awaiting_facilitation`, reviewer-host limits, terminal and effect state | keep `raw_governance` (not on the routine allowlist) | I |

## What changed

- `envelope_step.py`: a bounded `sync_state` lifts `warnings`, and a proceed
  that carries them is not routine. A lean search lifts `limit_clamped_from`.
  For #2443, a routine `request_review` omits `raw_governance` (see that
  commit and `tests/test_request_review_routine_ack.py`).
- `knowledge/handlers.py` `_lean_search_payload` keeps `imported_omitted` and
  `imported_omitted_hint`.
- `tests/test_envelope_required_fields.py` declares the required set per
  alias and fails for a new experience alias until its set is declared.
  Removing any of the three fixes fails it (mutation-checked).

## Left open

- `sync_state` mirror mode's `mirror[]` lines that have no lift (complexity
  calibration, pace, fleet trajectory health).
- `check_working_state` `thresholds`.
- Dead lifts: onboard `session_key`, outcome `recorded_at`, and lean search
  `search_mode_requested` / `operator_used` / `tag_filter_dropped` /
  `total_count`.
- The `sync_state` `identity_assurance` block on a weak binding (its effect on
  confidence now arrives as a warning).

None of these is an id the agent passes back. Each is a judgement about
signal value against the response budgets (#2332, #2437), so it belongs with
the #2443 levers rather than in this guard.
