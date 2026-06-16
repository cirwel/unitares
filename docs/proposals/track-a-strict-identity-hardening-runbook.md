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
