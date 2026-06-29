# Stakes-keyed gating (#775) — design of record

> **Design record.** A planning / RFC document kept as design provenance; it captures intent at a point in time and may lag the running code. For current behavior see [`UNIFIED_ARCHITECTURE.md`](../UNIFIED_ARCHITECTURE.md) and the runtime sources it points to.

**Status:** classification artifact LANDED + INERT; gate mechanism PARKED.
**Issue:** #775 (spun from #752, step 3). Sibling: #776 (trust→friction, step 4).
**Date:** 2026-06-28.

## What this PR lands

The **classification half** of #775 and nothing that gates:

- `src/mcp_handlers/stakes_table.py` — the authoritative stakes classification
  (the "load-bearing artifact" #775 names). Pure data + two pure functions
  (`get_action_stakes`, `is_high_stakes`) + `export_table()`. No imports from
  the dispatch/middleware stack; no asyncio/anyio/DB.
- `decorators.py` — a `requires_verdict` field on `ToolDefinition` (tool-level
  escape hatch, default `"baseline"`) and `get_call_stakes_requirement()`, the
  call-granular alias-aware resolver that mirrors `get_call_identity_requirement`.
- `tests/test_stakes_table.py` — table integrity, the load-bearing exemptions,
  a coverage drift-guard (every registered surface is a deliberate
  classification), and resolver semantics.

Nothing consults the resolver at runtime. This is the durable, port-survivable
slice; the gate mechanism is parked (see "Why the gate is parked").

## Why the gate is parked

Two findings from pre-implementation analysis (2026-06-28):

1. **There is no synchronous pre-action verdict.** A verdict (`safe`/`caution`/
   `high-risk` and an action like `approve`/`guide`/`risk_pause`) is computed
   **only** as the result of `process_agent_update`. Live DB confirms the last
   verdict is readable per agent (`state_json->>'verdict'`), but `margin` and
   `sub_action` are **not persisted** (NULL across 73k+ rows). So #775 cannot be
   "require a verdict to proceed" as literally framed — it can only be **"block a
   high-stakes action when the agent's _last_ posture was flagged"**: an
   eventual-consistency gate on the last check-in, stale by up to one check-in.
   Honest and useful, but weaker than the issue's wording. Any future gate must
   document this staleness rather than imply real-time evaluation.

2. **This surface is committed to a BEAM port (γ).** The 2026-06-25 roadmap
   resolution moved handler-dispatch + identity-middleware from *deferred* to
   *committed-to-design* (identity-middleware sequenced LAST — "worst first" —
   but the direction is BEAM, not Python). A Python middleware gate written now
   is **re-expressed in Elixir when the port lands**; the classification table is
   what survives. #775 itself says this is "make the default better," not a hole
   (the coverage hole was already closed by the #669 substrate-observation sink).
   So we land the durable artifact and wait for the port sequencing or real
   demand before building throwaway Python.

## The parked gate — blueprint (build when unblocked)

When the gate is built (Python now if demand appears, or Elixir at the port),
this is the reviewed design:

- **Reframe:** gate on the **last** governance posture, not a fresh verdict.
  Read it from the in-process monitor cache (`mcp_server.monitors[agent_id]`,
  O(1), synchronous, no DB — safe on the anyio dispatch path) with a durable
  DB fallback for cold monitors (`core.agent_state`, via ExecutorPool only —
  never a raw asyncpg await on the dispatch path; the existing `http_effect_veto`
  read at `http_api.py` is the precedent).
- **Allow policy** (mirror `http_effect_veto`): allow when last verdict is not
  `high-risk` AND last action ∈ {`approve`, `guide`, `proceed`}; otherwise block
  with a typed refusal.
- **No-history → ALLOW** (benefit of the doubt). This is the cooperative
  north-star, see below.
- **Typed refusal** parallel to `strict_identity_refusal_payload`:
  `status="verdict_required"`, `tool_class="high_stakes"`,
  `rollout_flag="UNITARES_VERDICT_GATE_REQUIRED"`, `next_step` pointing at
  `process_agent_update`. Single-sourced, success-shaped (not an MCP error) to
  avoid retry loops — same discipline as the #425 refusal.
- **Two enforcement points** (the identity gate is consulted at five sites
  total, but only two need a verdict check): a new middleware step in
  `PRE_DISPATCH_STEPS` **after** `resolve_identity` (needs `ctx.bound_agent_id`),
  and a REST-parity hook in `http_tool_service.execute_http_tool`. The REST path
  is the real unbound population (it transport-injects a synthetic
  `client_session_id`), so REST parity is mandatory, not optional.
- **Env-flag-off rollout** (`UNITARES_VERDICT_GATE_REQUIRED`), same staged
  burn-in discipline as `STRICT_IDENTITY_REQUIRED`.

### The load-bearing exemption

`process_agent_update` (alias `sync_state`) **must stay baseline**. It is the
call that *produces* the verdict; gating it on having a prior verdict
permanently blocks every new agent's first check-in. The table classifies it
`baseline`, and `test_process_agent_update_is_baseline` pins it. Same logic
exempts the identity-lifecycle tools (`onboard`, `identity`, `bind_session`,
`self_recovery`).

## Cooperative north-star (a real constraint, not flavor)

Sibling #776 wires `trust_tier` to gate friction. The standing guidance: trust
is keyed on **calibration / informativeness** (does self-reported confidence
match outcomes), never on mere **participation**. So:

- Absence of a check-in → **baseline** gate everyone faces, never *punitive
  extra* friction. "Non-participant → more gating" is the anti-pattern.
- The stakes gate's no-history branch returns **allow**, structurally preventing
  "punish the silent" from being expressible here.
- If #776 later modulates thresholds by calibration, it does so in a separate
  helper and may only make a *flagged* posture stricter — it may never turn
  absence-of-data into a block.

## Porting contract

`stakes_table.py` is data-only by design. At the BEAM port, do **not** re-derive
the classification by hand — serialize it:

```
python -m src.mcp_handlers.stakes_table --export-json
```

and load the JSON as Elixir config. `export_table()` is the single
authoritative serialization point.

## Classification summary

92 entries (22 high, 70 baseline). High = destructive/irreversible ops,
fleet/global mutations, governance-state changes applied to other agents,
single-writer surfaces, and dialectic resolution. Everything else is observed
by the substrate sink, not pre-gated. See `stakes_table.py` for the full list
and per-key rationale.
