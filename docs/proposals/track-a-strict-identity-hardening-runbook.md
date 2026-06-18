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

**Root cause (by elimination).** Under `STRICT_IDENTITY_REQUIRED`,
`process_agent_update` refuses a non-caller-proven write by returning the #425
typed-refusal payload wrapped in `success_response()` — HTTP 200, no `isError`,
no `error` key (`identity_bootstrap.py:strict_identity_refusal_payload` +
`updates/phases.py:334`). Those two strict-refusal returns are the *only* paths
in `process_agent_update` that yield a 200 success-shape without persisting:
every other early exit is an `error_response` (which the SDK raises on), and the
core update's `auto_save` increments `total_updates` synchronously. So a resident
that returns 200 and doesn't record is, necessarily, being strict-refused.

**Why it was invisible.** The SDK's `client.checkin()` didn't recognize the
refusal shape — no `isError`/`error`, so it defaulted the verdict to `proceed`
and returned a fake-success `CheckinResult`. The resident believed it checked in.
Fixed in the SDK (PR #824): `errors.extract_identity_refusal` now raises a typed
`IdentityRefusedError`, so the cycle fails loud (launchd log + `notify_on_error`)
instead of silently. **Execution step 2 / Done-criteria "watch for refusals"
cannot rely on the SDK surfacing them on a pre-#824 client — confirm the
resident's client carries that fix, or watch the server-side
`[PROCESS_UPDATE] STRICT refusing write` log line directly.**

**Why Chronicler and not the others.** The refusal only fires when identity
resolved `server_inferred` (transport fingerprint, not caller-proven) AND the
agent is not substrate-exempt. Chronicler is the longest-cadence resident
(daily), so its stored `continuity_token` is the most likely to go stale between
runs — a stale token still UUID-resolves but isn't caller-proven. Shorter-cadence
residents refresh before lapsing.

**The fix is enrollment, not `parent_agent_id`.** Pre-flight §2 says "add
`parent_agent_id`," but that is the remedy for *ephemeral* callers. A
substrate-anchored resident is exempted by `is_substrate_earned` (pure
row-presence in `core.substrate_claims`, plus the Pi allowlist) OR by the
`dedicated_substrate` predicate (`embodied`, or `persistent` + a server-findable
anchor). The durable fix for a refused substrate resident is to enroll it:

```bash
scripts/ops/enroll_resident.py \
  --agent-id <resident-uuid> \
  --launchd-label com.unitares.<name> \
  --executable <interpreter the plist launches> \
  --notes "strict-identity exemption"
```

`017_substrate_claims.sql` already names Vigil/Sentinel/Chronicler as expected to
carry rows; a missing row is the likely state for any resident that goes dark
here. Verify with
`SELECT agent_id, expected_launchd_label, enrolled_at FROM core.substrate_claims ORDER BY enrolled_at;`
and reconcile against the resident roster before flipping the flag wider.
