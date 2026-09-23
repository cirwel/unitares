# Track A — Strict identity hardening runbook

- **Status:** Ready to execute
- **Target:** UNITARES governance server (`CIRWEL/unitares`)
- **Goal:** Close the fingerprint-pin resume hole and enable strict identity
  refusals, so that UUID disclosure cannot be turned into identity hijack. This
  is the prerequisite for any operator-vision delegation (see ADR-001).

## The two flags

Both currently default to permissive `log` mode
(`config/governance_config.py`):

| Env var | Default | Strict behavior |
|---|---|---|
| `UNITARES_IDENTITY_STRICT` | `log` | Reject non-`pre_onboard` tool calls that lack a bound identity with a typed `identity_required` refusal, instead of auto-minting an ephemeral identity. |
| `UNITARES_SESSION_FINGERPRINT_CHECK` | `log` | On an IP/UA fingerprint mismatch during pin-fallback resume, fall through to a fresh mint instead of silently rebinding to the pinned UUID. |

Modes for each: `off` (emergency rollback) → `log` (observe, warn, allow) →
`strict` (enforce).

### Confirmed current state

- Code defaults: both `log`.
- Live probe: a session resolved via `"resolution_source": "ip_ua_fingerprint"`
  and auto-bound to an existing UUID with no ownership proof and no refusal →
  both gates non-strict in production.
- Partial enforcement already live: `required`-class tools (e.g. `call_model`)
  refuse with `identity_required` even now (the #425 staged rollout), while
  `pre_onboard` read tools still auto-bind. Strict closes the read/auto-bind gap.

## Pre-flight

1. **Inventory unbound callers.** In `log` mode the server emits
   `[IDENTITY_STRICT]` and `[PATH2_IPUA_PIN_RESUME]` / `identity_hijack_suspected`
   warnings. Pull recent occurrences; every distinct caller there will either
   refuse or re-mint under strict.
2. **Fix resident agents first.** Residents (Vigil, Sentinel, Watcher, Steward,
   Chronicler, Lumen) must declare lineage at onboard (`parent_agent_id`) or hold
   a substrate-earned identity so they survive the flip. Per project notes, fix
   offenders by adding `parent_agent_id` to their bootstrap before flipping.
3. **REST surface.** Confirm the REST gate is wired (it ships inert while the
   flag is off). Unbound REST reads currently succeed under strict; the
   dashboard's 30s read sweep is `pre_onboard`-classified and should pass, but its
   operator WRITE buttons require an operator credential under strict — verify the
   dashboard presents one.

## Execution (staged, reversible)

Roll each flag independently; do not flip both blind at once.

1. **Fingerprint check → strict.**
   ```
   UNITARES_SESSION_FINGERPRINT_CHECK=strict
   ```
   Restart / reload. Watch for legitimate residents falling through to fresh
   mints (would show as lineage breaks / new ghost UUIDs). If clean for a full
   resident cycle (longest cron is ~30 min; give it a few hours), proceed.

2. **Identity strict → strict.**
   ```
   UNITARES_IDENTITY_STRICT=strict
   ```
   Restart / reload. Watch for `identity_required` refusals on paths that should
   have been bound. Expected: previously auto-minted ephemeral callers now refuse
   until they `onboard()`.

3. **Verify.** Re-run the probe: a fresh, unbound call to a `pre_onboard` read
   tool should still work; a `required`-class call without onboard should refuse;
   a bare-UUID resume attempt should be denied; a fingerprint-mismatched resume
   should mint fresh rather than rebind.

## Rollback

Set the offending flag back to `log` (observe) or `off` (full pre-Part-C
behavior) and reload. No data migration is involved — these gate request-time
resolution only. Keep `off` reserved for emergencies; prefer `log` so telemetry
continues.

## Done criteria

- No legitimate resident refuses or loses lineage across a full cron cycle.
- Bare-UUID resume denied; fingerprint-mismatch resume mints fresh.
- Dashboard reads pass; dashboard writes carry an operator credential.
- `identity_hijack_suspected` warnings drop to zero for known-good callers.

After Track A is stable, proceed to Track B (`operator_delegate` design); do not
delegate operator vision to any agent before Track A is enforced.

## Council correction (2026-06-16) — the flag model above is imprecise

The second-pass council (see ADR-001 §"Council review") refuted this runbook's
"flip the flag and it's enforced" framing. Corrections, to apply on the next
revision:

- **There are two distinct flags this runbook conflates.**
  `STRICT_IDENTITY_REQUIRED` (boolean, default `false`, `identity_bootstrap.py`)
  gates auto-mint *refusal*; `UNITARES_IDENTITY_STRICT` (`IDENTITY_STRICT_MODE`,
  three-mode, default `log`, `governance_config.py:1019`) gates the bare-UUID
  resume path. They are not the same lever.
- **Neither flag governs identifier disclosure.** Redaction in `query.py` keys on
  `operator_caller` + `caller_uuid`, resolved independently of any strict flag.
  Flipping these flags changes *mint/resume* behavior, not who-sees-which-UUID —
  so Track A is a resume-hardening prerequisite, not a disclosure control.
- **`IPUA_PIN_CHECK_MODE` already defaults to `strict`** (`governance_config.py:1095`),
  so the "both default to `log`" claim is wrong for the pin-check leg.
- **REST/BEAM enforcement gap.** The REST strict gate
  (`http_tool_service._strict_identity_refusal_or_none`) only short-circuits
  auto-mint and is disclosure-blind; BEAM/Wave-3a routing runs with no identity
  middleware at all. Confirm the surface a caller lands on before assuming a flip
  reaches it.
- **Track B precondition tightened:** ship the delegate inert unless both resume
  gates are `strict`, enforced by a startup assertion — not a prose prerequisite.

## Incident (2026-06-17) — Chronicler silent-dark under strict

A real instance of the Pre-flight §2 risk, with two lessons the runbook above
missed. Chronicler (the daily-cadence resident) stopped recording EISV
check-ins after 2026-06-14 while its launchd job kept running cleanly: identity
resumed (`resumed via UUID …`), all metrics POSTed, every cycle ended on
`POST /mcp/ 200 OK`, and the launchd log showed no error. The server simply
recorded nothing — `total_updates` frozen — for three days.

> **Correction (2026-06-17, live-traced).** The first write-up of this incident
> (merged in #828) pinned the refusal at `updates/phases.py:334` and concluded
> "the fix is enrollment." Both were wrong. Live tracing against the running
> strict server showed the refusal fires *upstream* at the identity middleware
> (`session_resolve_miss` from `PATH2_RESUME_MISS`) — the call never reaches
> `phases.py:334` — and enrollment would make it *worse* (S19; see below). The
> corrected root cause and the shipped fix (PR #831) follow; the superseded
> enrollment remedy is removed.

**Root cause — a continuity-token TTL cliff.** `_CONTINUITY_TTL = 3600` (1h,
`identity/session.py:33`) is shorter than Chronicler's 24h cadence
(`StartInterval 86400`), so the continuity token in its anchor
(`~/.unitares/anchors/chronicler.json`) is *always* expired by the time the next
daily run resumes. The chain (verified in the live server log):

1. `identity(agent_uuid, continuity_token=<expired>, resume=true)` can't use the
   expired token, so it resolves via the **PATH 0 `agent_uuid` passthrough**
   (`[DISPATCH] PATH 0 passthrough … skipped resolution`). That reissues a fresh
   in-memory token but **mints no PG session row**.
2. The first `process_agent_update` carries no token (per #513 the happy path is
   token-free), so identity resolution finds no session for `agent-<uuid12>` →
   **`PATH2_RESUME_MISS`** (`identity/resolution.py:838`) → the middleware returns
   the #425 typed-refusal (HTTP 200, no `isError`/`error`). `phases.py:334` is a
   *second*, downstream write gate this call never reaches.

This is a time-dependence cliff (correctness carried by a freshness window the
agent can't sense) — the same class as the Watcher 24h-identity cliff (PR #595):
`failure_onset − last_success ≈ window`. Before strict, step 2's miss fell
through to a silent ghost-mint, masking the latent bug; strict closed the
fall-through and turned it into a hard refusal. (The original "200-without-persist
⇒ strict-refused" elimination still holds as a *symptom* test; it just named the
wrong refusal site.)

**Why it was invisible.** The SDK's `client.checkin()` didn't recognize the
refusal shape — no `isError`/`error`, so it defaulted the verdict to `proceed`
and returned a fake-success `CheckinResult`. The resident believed it checked in.
Fixed in the SDK (PR #824): `errors.extract_identity_refusal` now raises a typed
`IdentityRefusedError`, so the cycle fails loud (launchd log + `notify_on_error`)
instead of silently. **Execution step 2 / Done-criteria "watch for refusals"
cannot rely on the SDK surfacing them on a pre-#824 client — confirm the
resident's client carries that fix, or watch the server-side
`[PROCESS_UPDATE] STRICT refusing write` log line directly.**

**Why Chronicler and not the others.** It is purely cadence vs TTL. Chronicler's
24h interval exceeds the 1h continuity TTL, so its anchor token is *deterministically*
expired at every resume and the session row (24h `SESSION_TTL`) has also lapsed.
Shorter-cadence residents (Vigil/Watcher/Sentinel) check in well inside the 1h
window, so they keep both a live session row and a fresh token and never hit the
miss.

**The fix is the continuity-token rebind on check-in (PR #831), NOT enrollment.**
The shipped fix is SDK-side and respects #513's continuity-token narrowing: on a
refused `checkin()`, the SDK retries once presenting the fresh in-memory token
(reissued by the resume in step 1) as an explicit ownership re-proof. The server
honors it via the existing **PATH 2.8 token-rebind** (`identity/resolution.py:1009`),
which mints a caller-proven session (`proof_origin=caller_asserted`), clearing
both the resume-miss guard and the `phases.py:334` write gate. The token rides
the wire *only* on recovery; the happy path stays token-free.

> ⚠️ **Do NOT enroll an HTTP resident in `core.substrate_claims` to fix this.**
> The first write-up recommended `enroll_resident.py`; that backfires. The S19
> gate `SUBSTRATE_HTTP_REJECT` (`identity/resolution.py:313` and PATH 2.8 `:1025`)
> rejects a substrate-anchored UUID resuming over **HTTP** (it must attest over
> the UDS socket). Chronicler (and Vigil/Watcher) are HTTP MCP clients, so a
> `substrate_claims` row converts today's `session_resolve_miss` into a hard
> `substrate_anchored_uuid_requires_uds` refusal — strictly worse. Enrollment is
> only correct for residents that connect over UDS (currently Sentinel/BEAM).
> `017_substrate_claims.sql` naming Vigil/Chronicler as "expected" predates the
> S19 HTTP gate and should not be read as an instruction to enroll them.

Diagnostic, not remedy: a missing `substrate_claims` row is expected for HTTP
residents and is *not* the cause of a dark resident. To confirm the cliff
instead, decode the anchor token's `exp` and compare it to the cadence
(`failure_onset − last_success ≈ TTL`), and look for `PATH2_RESUME_MISS` for the
resident's `agent-<uuid12>` session key in the server log.
