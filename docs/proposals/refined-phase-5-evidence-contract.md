# Refined Phase-5 Evidence Contract

**Status:** SHIPPED — Phase 5 bootstrap-only observability query path landed via PR #188 (paired with `onboard-bootstrap-checkin.md`).
**Predecessor:** Calibration "honest absence" PR — surfaced the signal-starvation problem this spec resolves the supply side of.

## Revisions

**v3 (2026-04-26)** — third council pass; folded in:

- §4 — `peek_prediction` was a fabricated name; the actual non-destructive function is `lookup_prediction` (`monitor_prediction.py:35`). Renamed throughout.
- Risks table — stale "Default → 1800s" row contradicted §5b's 3600s decision; fixed.
- Test plan — clarified concurrency test is a regression canary, not a correctness assertion (lock fix deferred to a separate PR per code-review + dialectic).
- New §8 "Deploy gate" — `UNITARES_PHASE5_EVIDENCE_WRITE` env flag (default off → shadow → enable). Protects EISV class-conditional scales from a sudden distribution shift when step 4 starts flooding `outcome_events` with agent-reported rows. Per memory's `feedback_eisv-bounds-drift.md`.
- Implementation order — added one-liner clarifying steps 1–3 ship visibility, step 4 ships supply (gated by deploy flag).
- §9 numbering bump (Compatibility bridge was §8 in v2).

**v2 (2026-04-26)** — second council pass on the v1 spec surfaced concrete defects; folded in:

- §1 — explicit `kind` → `outcome_type` mapping (was hand-waved as `_classify_outcome_type`).
- §2 — pseudocode used dict `.get()` on Pydantic-validated objects; switched to attribute access. Added explicit `ctx.warnings → response_data["warnings"]` plumbing because warnings don't surface today. Added one sentence on calibrator weighting by `(verification_source, prediction_binding)` per dialectic.
- §4 — `ttl_expired_fallback` was unreachable as written (consume returns `None`, indistinguishable from "missing"); replaced with two-phase lookup-then-consume using a non-destructive `lookup_prediction` pre-check.
- §5a — `consume_prediction` is a module-level function, not a method; fixed the sketch.
- §5b — **TTL default is already 3600s** (`governance_monitor.py:167`), not 600s. Spec no longer proposes a bump; the change is the hard-on-consume check. Added explicit Lumen-class breakage disclosure per dialectic.
- §6 — strip happens in `response_formatter.py` (`_format_standard|minimal|compact|mirror`), not `update_response_service.py`. Spec retargets the patch.
- §7 — noted the seam is already end-to-end at the `outcome_event` tool level; the gap is only on the `process_agent_update` side.
- §"Risks" — reframed C as **the permanent floor, not a bridge**: server-verified outcomes (v2) cover ~30% of the calibration surface; the 70% (test runs, builds, file ops, external tool calls) is intrinsically agent-mediated and C is structurally required.
- §"Implementation order" — reordered to `1 → 2 → 6 → 3 → (4+5 squashed)` so `prediction_binding` echo doesn't strand without `prediction_id` being visible to the agent.
- §"Test plan" — added concurrency test for racing `outcome_event` calls on same `prediction_id`; added describe_tool drift test.

## Problem

The auto-calibration loop (`apply_confidence_correction` in `src/calibration.py`) requires fresh tactical evidence to correct agent-reported confidence. Today the tactical channel is starved — `tactical_evidence.last_updated` was 12 days stale at the time of writing — because nothing in the runtime emits `outcome_event` for normal agent work. The earlier "honest absence" PR makes the starvation visible (the corrector now returns identity when bins are >7d stale), but does not resupply the channel.

The Bash-hook proposal that surfaced first was rejected after council review: it would pair the agent's last self-reported confidence with an unrelated subsequent shell exit code, calibrating against the wrong joint distribution. The dialectic framing: not signal-starved, signal-poisoned.

This spec defines the replacement: a structured evidence contract that an agent populates in the same MCP call where the claim originates, so claim and truth-check share session context, identity binding, and an epistemic moment.

## Non-goals

- **Not** a Bash-tool hook in any agent-host plugin.
- **Not** a free-form regex parser over `response_text` (Design A from the brainstorm — rejected: hidden calibration behavior).
- **Not** server-side classification of arbitrary tool calls. Agents declare evidence; the server validates and records.
- **Not** server-verifiable outcomes from KG/dialectic/state-transitions — that is a deliberate v2 path, with the `verification_source` field added in v1 as the seam.
- **Not** a per-agent-class TTL table or Hermes session-lifecycle work — separate spec.

## Design

### 1. Canonical contract: `recent_tool_results` field on `process_agent_update`

New field on `ProcessAgentUpdateParams`:

```python
class ToolResultEvidence(BaseModel):
    """Self-reported evidence from a tool the agent just invoked.

    Self-report from agents IS the data source. The server treats
    this as `verification_source="agent_reported_tool_result"` and
    will be cross-checked by future server-verified primitives.
    """
    model_config = ConfigDict(extra="forbid")

    kind: Literal["command", "test", "lint", "build", "file_op", "tool_call"]
    tool: str = Field(..., max_length=64)
    summary: str = Field(..., max_length=512)
    exit_code: Optional[int] = None
    is_bad: Optional[bool] = None  # if exit_code is missing, agent must classify
    prediction_id: Optional[str] = None  # links to a prior process_agent_update mint
    observed_at: Optional[datetime] = None  # defaults to server receive time

class ProcessAgentUpdateParams(...):
    # ... existing fields ...
    recent_tool_results: Optional[List[ToolResultEvidence]] = None
```

Strict nested schema (`extra="forbid"`); unknown fields are a 4xx, not silently dropped. Per GPT council: "schema-enforced decomposability" is the value here, not truthfulness — the contract is inspectable at the API boundary, lintable, versionable.

**`kind` → `outcome_type` mapping.** The agent declares `kind` (what was the tool); the server derives `outcome_type` (what was the result, in calibration vocabulary):

| `kind`     | `is_bad` or `exit_code` | derived `outcome_type` |
|------------|-------------------------|------------------------|
| `test`     | false / 0               | `test_passed` |
| `test`     | true / non-zero         | `test_failed` |
| `command`, `lint`, `build`, `file_op`, `tool_call` | false / 0 | `task_completed` |
| `command`, `lint`, `build`, `file_op`, `tool_call` | true / non-zero | `task_failed` |

Only `test` outcomes hit the `record_tactical_decision` path (tightest calibration coupling); the rest land as generic completion outcomes. This matches the existing `outcome_events.py:266` gate.

### 2. Phase-5 server-side processing

> Note: the "Phase-5" name is borrowed from `auto_ground_truth.py` lineage; in `phases.py` the calibration-correction code lives in the "Validate Inputs" / `transform_inputs` region around line 430. This spec's iteration lands in the same enrichment block, immediately after `apply_confidence_correction`.

Pydantic has already validated `recent_tool_results` into a `List[ToolResultEvidence]` by the time Phase-5 runs. Use attribute access, not `.get()` — the latter would `AttributeError` on a model instance:

```python
for evidence in (ctx.recent_tool_results or []):
    try:
        # outcome_type derivation per §1 mapping table
        outcome_type, is_bad = _derive_outcome(evidence)
        await _emit_outcome_event_inline(
            agent_id=ctx.agent_id,
            outcome_type=outcome_type,
            is_bad=is_bad,
            prediction_id=evidence.prediction_id,
            confidence=ctx.confidence,  # post-correction
            verification_source="agent_reported_tool_result",
            detail={
                "tool": evidence.tool,
                "summary": evidence.summary,
                "kind": evidence.kind,
                "exit_code": evidence.exit_code,
                "observed_at": (evidence.observed_at or utcnow()).isoformat(),
            },
        )
    except Exception as e:
        logger.debug("Phase-5 evidence record failed for %s: %s", evidence.tool, e)
        ctx.warnings.append(f"evidence record failed for tool={evidence.tool}: {e}")
        # per-item isolation: one bad item must not abort siblings
```

**Per-item isolation rule:** a malformed item must not abort the siblings (code-review #3 risk). Wrap each item in try/except; append a per-item failure to `ctx.warnings`.

**Surface `ctx.warnings` in the response.** Today `ctx.warnings` is populated (e.g., the identity-assurance dampening at `phases.py:443`) but never copied into the agent-visible response — the formatter modes don't read it. Spec adds: `build_process_update_response_data` merges `ctx.warnings` (de-duped) into `response_data["warnings"]: List[str]`, and each `_format_*` function in `response_formatter.py` preserves the key (alongside the §6 `prediction_id` plumbing — same surface, same diff).

**Calibrator weighting (per dialectic).** Phase-5 records the `(verification_source, prediction_binding)` pair on each outcome row. Calibrator weighting by this pair is in scope for v1; **default uniform until measured**. The pair is captured so future weighting can be data-driven rather than guessed.

### 3. `verification_source` enum on `outcome_event`

Add to `OutcomeEventParams` and propagate through `db.record_outcome_event`:

```python
verification_source: Literal[
    "agent_reported_tool_result",  # v1 default — this spec
    "server_observation",          # v2 — server-verified outcomes (KG writes, dialectic verdicts, state transitions)
    "external_signal",             # CI webhook, monitoring system
] = Field("agent_reported_tool_result", description="...")
```

Stored on the outcome row. Calibrator can later weight or filter by source. This is the seam the dialectic agent recommended: "deprecate C's contributions without rewriting the calibrator" when v2 server-verified outcomes land.

### 4. Echo `prediction_binding` on `outcome_event` response

Today every misuse of `prediction_id` (fake, stale, replayed, wrong-agent) returns `success: true` and silently falls through to `_prev_confidence` or audit-trail fallback. Verifier called this "the only signal is in DB; not in API response."

Add to `outcome_event` response payload:

```python
"prediction_binding": Literal[
    "registry",                  # the supplied prediction_id was found and consumed
    "ttl_expired_fallback",      # supplied id was found but past TTL — see §5
    "missing_prediction",        # supplied id was unknown (never registered or already consumed)
    "argument_fallback",         # no id supplied; used the explicit confidence arg
    "prev_confidence_fallback",  # used monitor._prev_confidence
    "audit_trail_fallback",      # used db.get_latest_confidence_before
    "no_binding",                # all fallbacks failed; calibration NOT recorded
]
```

**Two-phase resolution to make `ttl_expired_fallback` reachable.** As written today, `consume_prediction` returns `None` for both "stale" and "missing" — the caller can't distinguish. Spec adds a non-destructive `lookup_prediction(open_predictions, prediction_id)` (already exists at `monitor_prediction.py:35-45`) and uses it in `outcome_events.py` BEFORE calling `consume_prediction`:

```python
record = lookup_prediction(open_predictions, prediction_id)
if record is None:
    binding = "missing_prediction"   # never existed or already consumed
elif _is_expired(record, ttl_seconds):
    binding = "ttl_expired_fallback"
    # do NOT consume; let caller fall through to the next confidence source
else:
    consumed = consume_prediction(open_predictions, prediction_id)
    binding = "registry" if consumed else "missing_prediction"
```

This keeps `consume_prediction` simple (no signaling channel needed); the discrimination lives one layer up where the binding label is computed.

Agent sees immediately whether their `prediction_id` actually bound. Silent degradation becomes visible.

### 5. Hard TTL enforcement at the consume layer

Today TTL is enforced only inside `register_tactical_prediction` (which calls `expire_old_predictions` on each register). Verifier confirms `consume_prediction` itself does no TTL check. Result: a `consume_prediction` against a stale-but-unswept id will succeed and return its confidence, silently. Combined with the §4 misclassification, callers can't tell.

`consume_prediction` is a module-level function in `src/monitor_prediction.py:48-64` taking `(open_predictions, prediction_id)`. Two layered changes:

#### 5a. Add TTL parameter to `consume_prediction`; check it

```python
def consume_prediction(
    open_predictions: Dict[str, Dict],
    prediction_id: str,
    *,
    ttl_seconds: float = 3600.0,
) -> Optional[Dict[str, Any]]:
    if not prediction_id:
        return None
    record = open_predictions.get(prediction_id)
    if not record or record.get("consumed"):
        return None
    age = _time.monotonic() - float(record.get("created_at", 0.0))
    if age > ttl_seconds:
        # leave the record in place for peek-based discrimination (see §4)
        return None
    record["consumed"] = True
    return dict(record)
```

Caller in `outcome_events.py` passes `ttl_seconds=monitor._prediction_ttl_seconds` so the live config knob (`governance_monitor.py:167`) controls behavior.

#### 5b. TTL default — keep at 3600s; do NOT bump

Verifier corrected the v1 spec: live default is **3600s** at `governance_monitor.py:167`, not 600s as the v1 spec assumed. The earlier proposal to bump 600→1800 would have been a *reduction*. Spec keeps the live default unchanged.

The 3600s default still leaves long-cadence agents broken-by-default. **Lumen and other slow-cadence substrate-anchored agents will systematically hit `ttl_expired_fallback` under this default** because their natural quiet windows exceed 1 hour. Per-agent override via `monitor._prediction_ttl_seconds` is the v1 escape hatch; the per-agent-class TTL table is the v2 fix (separate spec).

The §4 `prediction_binding` echo is what makes this breakage *visible* rather than silent — Lumen-class agents will see `ttl_expired_fallback` in their outcome responses and operators can decide whether to override TTL or restructure prediction-emit timing.

### 6. Expose `prediction_id` in default response mode

Verifier corrected the v1 spec: `update_response_service.py:33-36` already injects `prediction_id` unconditionally onto `response_data`. The strip happens later in `src/mcp_handlers/response_formatter.py` — `_format_standard`, `_format_mirror`, `_format_minimal`, `_format_compact` each rebuild the response from scratch and don't pass `prediction_id` through. Only `full` mode preserves it.

The fix targets the formatter, not the service:

- `_format_standard` (default for explicit standard mode) — pass `prediction_id` through
- `_format_mirror` (default for disembodied agents) — pass `prediction_id` through
- `_format_compact` — pass `prediction_id` through
- `_format_minimal` — leave stripped (this mode is explicitly bandwidth-constrained)

Same diff also adds `warnings` preservation per §2 — both fields share the same plumbing change in each formatter, easier as one commit than two.

Also update `describe_tool("process_agent_update")` to document `prediction_id` in the returns block.

### 7. Sequential-calibration docstring fix

`src/sequential_calibration.py:36-47` has a docstring claiming "no prediction_id seam yet... A prediction_id seam is phase-two work." Verifier and code reviewer both confirmed the seam is wired end-to-end at the `outcome_event` tool level today (`outcome_events.py:171-225` consumes `prediction_id` and forwards to `record_exogenous_tactical_outcome`). The seam is NOT phase-two; it's operational.

The actual gap this spec closes is on the `process_agent_update` side: agents have no contract to *report* tool outcomes that would mint and consume those predictions. Update the docstring to: (a) note the consume path is live; (b) point to this spec for the report path; (c) drop the "phase-two" framing.

### 8. Deploy gate

Per memory's `feedback_eisv-bounds-drift.md`: EISV class-conditional scales were measured against the current sparse mix of `outcome_events` rows. Step 4 (the `recent_tool_results` Phase-5 iteration) will start writing rows at significantly higher volume — every reported tool outcome becomes one new row, with `verification_source="agent_reported_tool_result"`. If those rows shift the sample distribution before the calibrator's correction logic is re-measured, the bounds-drift invariant is at risk.

Add an env flag `UNITARES_PHASE5_EVIDENCE_WRITE` controlling step 4 behavior:

| Mode | Phase-5 behavior |
|---|---|
| unset (default) | iterate `recent_tool_results` but skip the `outcome_event` write; log per-item counts only |
| `shadow` | write rows with `verification_source="agent_reported_tool_result"` AND a `detail.shadow_write=true` flag; calibrator excludes shadow rows from correction math |
| `1` / `enable` | full write; rows participate in calibration |

Deploy sequence:
1. Ship step 4 with flag unset → operators see the count of would-be writes per check-in. Calibrator unchanged.
2. Flip to `shadow` for 48h → distribution comparison: `(agent_reported, shadow)` vs current sparse mix. Operators inspect the bin shifts before live writes.
3. Flip to `1` once distributions look acceptable.

The flag is an operational seam, not a permanent feature — once the v2 server-verified primitive lands and class-conditional scales are re-measured against the broader mix, the flag can be retired.

### 9. Compatibility bridge (Design B as parser-into-internal-model)

Out of scope for v1 unless a real client emerges that cannot update its tool schema. If/when needed: a regex parser for `<eisv-evidence>{...}</eisv-evidence>` blocks in `response_text` produces the same `ToolResultEvidence` records the structured field would. Single internal model. Marked `compatibility-only` in code comments. **Not** implemented in v1 to avoid two surfaces drifting before there's a concrete need.

## Risks the council surfaced and how this spec handles them

| Risk | From | How handled |
|---|---|---|
| Pairing wrong joint distribution (last-confidence ↔ unrelated tool exit) | dialectic | Same-MCP-call binding; `prediction_id` links specific (confidence, timestamp) pair |
| Agent self-report can be fabricated | dialectic, GPT | `verification_source="agent_reported_tool_result"` flagged on every record; server-verified primitive deferred but seam is in place |
| Per-item failure aborts siblings | code-review | Try/except per item; surface failures via `ctx.warnings` |
| `extra="forbid"` collision with existing payloads | code-review | grep fleet for any existing `recent_tool_results` field name (none expected) |
| Silent degradation of `prediction_id` misuse | live verifier | New `prediction_binding` echo |
| TTL bleed (lazy enforcement) | live verifier (refined by user pressure) | Hard check on consume |
| TTL too short for slow agents | live verifier | Live default is already 3600s (verifier corrected v1); per-agent override is the escape hatch; per-agent-class TTL table is v2 work |
| Existing fleet (Vigil, Sentinel, Watcher, Steward, Chronicler, Lumen) breaks on schema change | code-review | `Optional` field, default `None`, old clients no-op (verifier grep confirms zero collisions) |
| `ttl_expired_fallback` was unreachable as v1-written | code-review (round 2) | Two-phase peek-then-consume in §4 makes the discrimination computable |
| `ctx.warnings` populated but not surfaced in response | code-review (round 2) | §2 + §6 add `warnings` plumbing through formatters |
| Pseudocode used `.get()` on Pydantic-validated objects (would AttributeError) | code-review (round 2) | §2 switched to attribute access |
| Lumen-class agents will systematically hit `ttl_expired_fallback` at 3600s default | dialectic + verifier | Documented in §5b; `prediction_binding` echo makes it visible; per-agent override is the v1 escape hatch |
| Production calibration shift when Phase-5 starts flooding `outcome_events` with `agent_reported_tool_result` rows; EISV class-conditional scales were measured on the current sparse mix | dialectic (round 3) | §"Deploy gate" — `UNITARES_PHASE5_EVIDENCE_WRITE` env flag (default off); enable shadow-write first, compare distributions for 48h, then enable correction-write |
| Race on `consume_prediction.consumed` flag (two simultaneous outcome_events for same prediction_id) | code-review + dialectic (round 3) | Lock not added in v1; concurrency test is a regression canary not a correctness assertion; documented in test plan |
| C might be transient — server-verified outcomes will replace it | dialectic (round 1) → corrected (round 2) | C is the permanent floor: ~70% of calibration signal is intrinsically agent-mediated (tests, builds, file ops, external tool calls). Server-verified covers ~30% (KG writes, dialectic verdicts, state transitions). v2 is a partial-coverage upgrade, not a replacement |

## Test plan

- Unit: `ToolResultEvidence` Pydantic validation (well-formed, missing required fields, extra fields rejected, malformed exit_code)
- Unit: per-item isolation — list of 5 items with item 3 malformed records items 1-2 and 4-5
- Unit: hard TTL on `consume_prediction` — predictions >TTL return None even if no sweep has fired
- Unit: `prediction_binding` echo correctness for each enum value (mock each fallback path)
- Integration: end-to-end `process_agent_update` with `recent_tool_results` advances tactical_evidence.eligible_samples by N
- Integration: agent that registers a prediction at T=0, references it at T=1800s (within 3600s TTL) → `prediction_binding == "registry"`
- Integration: agent that references a prediction_id at T>3600s → `prediction_binding == "ttl_expired_fallback"`
- Integration: agent that omits `prediction_id` → `prediction_binding == "argument_fallback"` or `"prev_confidence_fallback"` depending on arg presence
- Schema migration test: existing `process_agent_update` calls without `recent_tool_results` continue to work (Vigil/Sentinel/Watcher/Steward/Chronicler call signatures)
- Docstring drift test: `sequential_calibration.py` docstring no longer claims the seam is unimplemented
- Concurrency test (regression canary, NOT a correctness assertion): two simultaneous `outcome_event` calls referencing the same `prediction_id` — current `consumed` flag is not lock-protected, so both might read `False` and proceed. Test documents current behavior (at most one resolves to `prediction_binding == "registry"` under typical scheduling; the second resolves to `missing_prediction`). The lock fix is explicitly deferred to a separate PR; this test exists to catch regressions if the race becomes higher-probability under future async scheduling changes.
- `describe_tool` drift test: parametrize over schema fields and assert each is documented in the `RETURNS` block of `describe_tool("process_agent_update")`. This catches the original "stale docstring" failure class that triggered this whole spec round.
- `ctx.warnings` round-trip test: a Phase-5 evidence record that throws appends to `ctx.warnings`, and the resulting response payload includes the warning string. Covers the formatter-side regression risk.
- `prediction_binding` table tests: parametrized over `(prediction_id state, confidence arg state, prev_confidence state, audit-trail state)` enumerate which fallback fires and assert the correct binding label. Mock each layer independently.

## Implementation order

Per dialectic: ship pieces such that each step's surface is safe to live with if the next step delays. Step 1 must not strand without §6 (agents would have a binding-echo for an id they can't see). §4 + §5 are squashed because the field accepts data the server otherwise drops on the floor.

**Read this as: steps 1–3 ship visibility/enforcement (calibration unchanged); step 4 ships supply (calibration starts to change, gated by §8 deploy flag).** Operators will not see calibration behavior shift until step 4 is deployed AND the env flag is flipped past `shadow`.

1. **`prediction_binding` echo + hard TTL on `consume_prediction`** — both are pure-additions to `outcome_event` response/behavior; the binding label and TTL check together make `prediction_id` misuse visible. (Bundles old steps 1+2 because they're meaningless apart.)
2. **Expose `prediction_id` + `warnings` in formatter modes** (was old step 6). Now agents can actually USE the binding echo from step 1. Same diff plumbs `ctx.warnings → response_data["warnings"]`.
3. **`verification_source` enum on `outcome_event`**. Schema-only addition; default `agent_reported_tool_result`. Backward-compatible.
4. **`ToolResultEvidence` model + `recent_tool_results` field + Phase-5 iteration in `phases.py`** (squashes old steps 4+5). Schema and the consumer ship together so the field never accepts data the server silently drops.
5. **Update `describe_tool("process_agent_update")` returns block** (now includes `prediction_id` + `warnings` + `recent_tool_results` documentation).
6. **`sequential_calibration.py` docstring cleanup** — fix the "phase-two" framing per §7.
7. **Tests in same commits as their behavior changes** (per `feedback_tests-with-fixes.md`).

Each step lands as its own commit on the same branch — incremental, reviewable, individually revertable. Final ship via `scripts/dev/ship.sh` after the full suite passes (`./scripts/dev/test-cache.sh`).

## Future work explicitly out of v1 scope

- **Per-agent-class TTL table** (Lumen 6h, Vigil 1h, etc.) — separate spec; needs Hermes session-lifecycle decision first.
- **Hermes adapter `on_session_end` cleanup** — separate spec; folded into the same per-agent-class work.
- **Server-verified outcomes primitive** (`verification_source="server_observation"`) — separate spec; v2 deprecation path for agent-reported.
- **Performative-continuity contract rule** — separate spec; deeper question about which agent-class continuity claims the calibrator can rely on.
- **Design B regex parser** — only if a concrete client emerges that cannot update its tool schema.
