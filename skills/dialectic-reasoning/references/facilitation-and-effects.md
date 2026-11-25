# Dialectic Provenance, Wait Assessment, Facilitation, and Resolution Effects

Reference companion to `skills/dialectic-reasoning/SKILL.md`. Load when filing
detailed `reviewer_provenance`, interpreting `wait_assessment`, handling
`awaiting_facilitation` / `reassign` / `rejection_stands`, or reconciling
interrupted resolution effects and `attestation` blocks.

## Outside Consult Provenance and `paused_agent_state`

- **Consult limits and authority**: `dialectic(action="consult")` carries no
  authority: it never advances phase, never counts as a verdict (`agrees` stays
  `NULL` on the row; position is stored in `observed_metrics.consult.position`),
  and never refreshes the session's liveness clock, so it cannot overturn a
  standing rejection or keep a stalled session open. The server always stamps
  `reviewer_kind: "external_consult"` and records whether the filer is the
  paused agent, the reviewer, or a third party. A session keeps at most 8
  consults.
- **Provenance schema**: `reviewer_provenance` is descriptive, not identity
  proof — stamp the backend and model that actually ran. Beyond `backend`,
  `model_used`, and `consult_source`, accepted keys include `model_requested`,
  `models_used`, `tokens_used`, `cost_usd`, `latency_ms`, `finish_reason`,
  `fallback_from`, `warnings`, and `consulted_at`. Unrecognized keys are dropped
  (listed in the response), and string values truncate at 200 characters.
- **Why `consult` exists**: prior to interface contract 1.24.0 (2026-09-28),
  outside reviews had no seat once an orchestrated reviewer claimed the
  antithesis slot, leaving external consults uncounted (`0` of 669 measured
  dialectic messages on 2026-08-28).
- **No-judgment vs. degraded**: a model that judged and then failed output
  formatting is `degraded: true` and HAS judged — file the objection. Filing a
  non-verdict claims the single reviewer slot and blocks the paused agent behind
  unactionable text; pass `judgment_formed: false` on `antithesis` instead to
  record an abstention without claiming or changing slot ownership (the slot
  remains OPEN only if unassigned).
- **Compact vs. full session reads**: `dialectic(action="get", fields="compact")`
  previews long topic/reason text and returns executable `full_session_retrieval`
  arguments; omitting `fields` or passing `fields="full"` returns the full
  transcript.
- **Server-captured pause evidence**: an independent reviewer receives
  server-captured pause evidence separately from the paused agent's narrative in
  a full `dialectic(action="get")` read as `paused_agent_state`. Treat
  measurement, policy, and enforcement provenance (`risk_score_source`,
  `verdict_source`, `verdict_resolution_source`) as context; legacy `C(V)` is
  diagnostic controller feedback, not independent evidence. If continuity looks
  suspect, verify with `identity(client_session_id=...)`.
- **Authorship status**: `reviewed_work_authorship.status` reports `declared`,
  `not_supplied`, or `invalid`. A malformed stored marker fails closed for
  independent assignment.

## `wait_assessment` and `check_timeout`

`expected_by_s` derives from the orchestrated reviewer's configured ceiling
(`UNITARES_DIALECTIC_CODEX_TIMEOUT_S` plus spawn/reconsider overhead):

- `too_early`: wait is unremarkable — **not** proof the reviewer is alive
  (`poll_after_s` gives remaining budget to wait on).
- `overdue`: `poll_after_s` is `null` — evidence to investigate or offer
  facilitation (look, do not blindly poll or assume the reviewer is dead).
- `null` (`assessment: null`): read `note` rather than inferring health or
  trouble. Covers four distinct cases:
  1. Nobody owes a reviewer turn (terminal session, your turn, or OPEN slot).
  2. Reviewer is human or unmanaged (no declared budget).
  3. Transcript clock is missing or unreadable.
  4. Transcript entries are not in causal order.
- `dialectic(action="get", session_id="...", check_timeout=true)` is a **write**
  (can auto-reassign or flip phase) and is silently ignored for unbound callers.
  An OPEN antithesis slot is an invitation any eligible non-author agent may
  claim, not a stall.

## Facilitation, `rejection_stands`, and Reviewer Reassignment

- **`awaiting_facilitation`**: raised when no eligible reviewer remains, or when
  a session stalls in SYNTHESIS while the reviewer owes the move (first
  synthesis verdict owed, or paused agent answered a standing objection). This
  is a paused request for human help, not a verdict; a timeout sweep can later
  mark it `failed` without declaring a winner.
- **SYNTHESIS stall rules**: the sweep does not auto-reassign in SYNTHESIS
  because the protocol requires the same reviewer to revise its own verdict.
  Once `awaiting_facilitation` is raised, `check_timeout=true` can replace a
  paused or missing reviewer (an active reviewer waits for operator `reassign`).
  A SYNTHESIS stall where the paused agent owes the move raises no facilitation
  request and fails at the stuck threshold. A raised request is not withdrawn
  when the reviewer later replies (keeps the 4h hold).
- **`rejection_stands`**: a facilitation request over a standing rejection does
  not wait indefinitely. Once the reviewer has had its last word (answered the
  paused agent's reply and still rejects, or the orchestrated reviewer's
  continuation window ends), the sweep closes the session `failed` with
  `resolution_json.reason = "rejection_stands"` and clears the request.
  `verdict_acceptance` (`accepted`, `contested`, `no_reply`) is descriptive only
  (`no_reply` is not assent). Unmanaged/human reviewers close this way only by
  answering a reply and still rejecting.
- **Privileged `reassign`**:
  ```
  dialectic(action="reassign", session_id="<id>", new_reviewer_id="<agent-id>", reason="...")
  ```
  Allowed only for an authenticated operator or the currently assigned
  independent reviewer handing off its own slot. Can revive an
  `awaiting_facilitation` session (even one failed by a timeout sweep), but not
  a `rejection_stands` closure.
- Note: `escalate` and `cooldown` remain in the `ResolutionAction` enum only as
  recommendation labels in the model-assisted tool; no live path emits them.

## Interrupted Resolution Effects (Operators) and Attestation

- A terminal decision and its runtime mutation are linked by a durable
  resolution-effect record. Operators inspect with
  `dialectic(action="reconcile_effect", session_id="...")`:
  - `apply_pending` claims an untouched pending effect.
  - An `applying` effect is never replayed automatically: confirm its worker
    stopped, fence the exact `effect_owner_token` with `mark_uncertain`, inspect
    and repair live state, then call `mark_applied` with a reason and the exact
    `effect_updated_at` copied from the `unknown_partial` inspect response
    (compare-and-set; re-inspect if changed; already-applied retries are
    idempotent; never erases uncertainty evidence).
  - The durable effect row is authoritative; outcome, calibration, and lifecycle
    signals are best-effort and never make an applied effect replayable.
- **`legacy_unbound_session`**: a session predating pause-generation binding
  closes as an applied no-op without lifting the current pause; open a fresh
  review instead.
- **`attestation` block**: resolved session reads report `bilateral`,
  `single_signer`, `unsigned`, or `legacy_v1`. Party-HMAC signing of resolutions
  was retired on 2026-09-25, so new resolutions are `unsigned` with
  `unsigned_by_design: true` (`signature_version` 3 — naming the scheme, not a
  signing act). This is expected and does not weaken the transcript-governed
  conditions.
