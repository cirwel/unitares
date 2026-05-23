# S1 — `continuity_token` retirement plan

**Date:** 2026-04-24
**Scope:** Plan doc for `continuity_token` retirement per `plan.md` §Track C C1. Inventories current roles of the token, identifies what S11 already accomplished, surfaces the Part-C coupling that makes naive retirement unsafe, and lays out a sequenced deprecation path with operator decision points.
**Stance:** Descriptive + recommendation. No code changes in this pass.
**Unblocks:** S2 (auto-resume retirement), S3 (cross-channel retirement). Tightly coupled to S9 (PATH 1/2 anti-hijack re-scope) via the Part-C proof-of-ownership thread, and coupled to `bind_session` (which shares the same acceptor).
**Review provenance:** drafted 2026-04-24, reviewed in one pass by `dialectic-knowledge-architect` (ontology stress-test) and `feature-dev:code-reviewer` (call-site accuracy) before landing. Key review findings folded in: factual fixes to call-site citations; honest relabeling of what a TTL-only mechanism actually achieves (§4); expanded threat-model coverage (§7); `ownership_proof_version` forward-compat field added to S1-a (§4.5). **Late revision (2026-04-24 evening):** after R1 v3 design doc (`docs/ontology/r1-verify-lineage-claim.md`) landed mid-drafting, Option B was withdrawn — R1 is plausibility scoring for honest over-claims, explicitly not authentication; it does not replace the token's anti-hijack role. R1 becomes a complementary signal, not a substitute.

## TL;DR

`continuity_token` plays **four** distinct roles, not one. S11 (2026-04-21) retired role (1). Roles (2), (3), (4) remain:

| # | Role | Status |
|---|---|---|
| 1 | Plugin-cache auto-resume credential | **Retired 2026-04-21** (plugin #17, schema v2) |
| 2 | Server-issued resume token (onboard/identity emit + accept) | Active |
| 3 | PATH 0 bare-UUID anti-hijack proof-of-ownership | **Load-bearing** (Part-C Invariant #4, 2026-04-18) |
| 4 | `bind_session` resume input | Active |

**The core tension:** Identity Honesty Part C (2026-04-18, `docs/CHANGELOG.md:35-43`) made `continuity_token` load-bearing: PATH 0 bare-UUID resume now requires a `continuity_token` whose signed `aid` claim matches the requested `agent_uuid`. Before Part C, the token was a resume convenience; after Part C, it is the mechanism standing between "Lumen's UUID" and "any process that knows Lumen's UUID can speak as Lumen." Residents (SDK `GovernanceAgent`, Watcher) load their saved token before PATH 0 calls to stay strict-mode-compatible.

**What a TTL shrink actually buys (honest accounting).** An HMAC-signed short-TTL token proves one thing: the holder possesses a secret the server minted in the last N seconds. It does not prove process-instance continuity — the same token can be read from disk or memory by any co-resident process on the issuing host. "Process-instance proof" in the strict sense requires a PID-or-nonce-bound claim the server can verify as process-scoped. Option A (below) is therefore honestly labeled as **"performative, narrowed"** — a smaller version of the same possession-proof mechanism, not a transition to earned continuity.

`identity.md:111` flags token as retire/repurpose, but was written before Part C and does not yet account for the anti-hijack role. This doc is the reconciliation.

## 1. Current role inventory

### 1a. Issuers (server-side)

| Site | File:line | Role |
|---|---|---|
| `create_continuity_token` | `src/mcp_handlers/identity/session.py:57` | HMAC-signed token mint. TTL = 30 days (`_CONTINUITY_TTL` at L22). |
| Shared response-payload helper | `src/services/identity_payloads.py:17, 41-42, 53-54, 63-64` | `build_identity_response_data` + `build_onboard_response_data` — the payload builders both onboard and identity use to emit the token field |
| `onboard`/`identity` emit call sites | `src/mcp_handlers/identity/handlers.py:480` (shared diag helper) and `:917` (`handle_identity_adapter`) | Call `create_continuity_token` and embed in response via the shared payload builder |

### 1b. Acceptors (server-side)

| Site | File:line | Role |
|---|---|---|
| HTTP onboard direct-tool path | `src/http_api.py:276, 304, 307` | Resolves token before tool dispatch |
| MCP middleware (identity step) | `src/mcp_handlers/middleware/identity_step.py:219` (extract), `:297-308` (Part-C strict-mode gate), `:393-396` (PATH 2.8) | Request-scoped identity resolution |
| `resolve_continuity_token` | `src/mcp_handlers/identity/session.py:124, 275-284` | Core verification (HMAC + expiry + channel claim) |
| `onboard` handler | `src/mcp_handlers/identity/handlers.py:130` (IPUA pin), `:543-547` (Part-C `aid` match) | Resume-preferred path; Part-C gate |
| `identity` handler | `src/mcp_handlers/identity/handlers.py:807-808` | Token extraction; same gate machinery |
| `bind_session` handler | `src/mcp_handlers/identity/handlers.py:1034` (`handle_bind_session`), direct call to `resolve_continuity_token` at `:1066` | Third consumer per `identity.md:111`. **TTL change in S1-a silently changes `bind_session` acceptance semantics.** See §7 coupling note. |
| `process_agent_update` | `src/mcp_handlers/updates/phases.py:33, 145, 148` | Identity-strictness hint surface (`continuity_token for strong identity continuity`) |

### 1c. Secret management

| Surface | Location |
|---|---|
| HMAC secret env var | `UNITARES_CONTINUITY_TOKEN_SECRET` (falls back to `UNITARES_HTTP_API_TOKEN`, then `UNITARES_API_TOKEN`) |
| Provisioning | `scripts/ops/com.unitares.governance-mcp.plist:81` |
| Rotation | `scripts/ops/rotate-secrets.sh` (anchor strip at L70: drops cached token + client_session_id, keeps agent_uuid) |
| Diagnostic surface | `continuity_token_support_status()` in `session.py:25-33`; exposed via `identity()` response |

### 1d. Client-side consumers

**Unitares repo:**
- `scripts/unitares` (CLI) — L62 is inside the write heredoc (`_py_write_session`); L70, 148, 412 are the read + auto-pass paths
- `scripts/client/onboard_helper.py` — prefers cached token in onboard arguments (L181-184), writes token back on response (L211, 223)
- `hooks/session-start:311-315, 376-379` — reads token from onboard response, surfaces `Continuity token: supported` in banner
- `agents/sdk/src/unitares_sdk/agent.py` — SDK `GovernanceAgent` loads saved token for PATH 0 (L203-204)
- `agents/watcher/agent.py` — Watcher mirrors the SDK pattern

**Plugin repo (unitares-governance-plugin):**
- `unitares-governance-plugin/scripts/onboard_helper.py` — plugin's onboard wrapper (same behavior as unitares-side `scripts/client/onboard_helper.py`)
- `unitares-governance-plugin/scripts/checkin.py` — passes `continuity_token` on every check-in (`tests/test_checkin_helper.py:31-223`)
- `tests/test_post_identity_hook.py:94-97, 208` — **enforces v2 cache schema has empty `continuity_token`** (S11 landing)
- `tests/test_auto_checkin_decision.py:155-169` — `test_prefers_continuity_token_but_falls_back_to_session_id` pins the fallback hierarchy
- `tests/test_session_cache_perms.py:31, 55, 60` — cache secret-writeback perms test

**External:** Grep of `unitares-core` (archived), `unitares-discord-bridge`, `anima-mcp` — zero references as of 2026-04-24. **Caveat:** this only closes out consumers in repos known to the operator. Silent-break clients (personal scripts, forked helpers, cached tokens in user shell history) cannot be detected by grep and will not surface through the grace-period warning either — see §5 and §6.

### 1e. Documentation

- `docs/CHANGELOG.md:35-43, 91, 104, 124` — Part-C additions + HTTP resolution + identity response shaping
- `docs/ontology/identity.md:23, 85, 111` — v2 ontology flags token for retire/repurpose (written pre-Part-C; see §3 for reconciliation)
- `docs/operations/OPERATOR_RUNBOOK.md:70` — "still work as legacy fallbacks for external/ephemeral clients but are not needed for resident agents"
- `README.md` / `CODEX_START.md` in plugin — onboard-helper guidance preferring token

## 2. What S11 already accomplished (2026-04-21)

Per `plan.md:47` and the S11 execution appendix (plan.md:416-424):

- **Plugin cache (`unitares-governance-plugin` post-identity hook) no longer writes `continuity_token` to `.unitares/session.json`.** Schema bumped to v2; v1 cache files with populated token are **ignored on read** (`test_session_start_checkin.py:264-275`, `test_post_identity_hook.py:94-97`).
- **SessionStart banner inverted** to lead with `onboard(force_new=true, parent_agent_id=<cached UUID>, spawn_reason="new_session")`.
- **S1 deprecation breadcrumb** comment planted in `onboard_helper.py` noting the token is a compatibility surface for external clients.

**What S11 did not touch:** server-side emit, server-side accept, PATH 0 proof-of-ownership, `bind_session`, the CLI (`scripts/unitares`), or the check-in helper's token-pass behavior. The plugin cache was the write-path surface S11 owned; S1 owns the rest.

## 3. The Part-C coupling (why this is not pure deprecation)

`docs/CHANGELOG.md:35-43` — Identity Honesty Part C, 2026-04-18, gated by `UNITARES_IDENTITY_STRICT`:

> **PATH 0 bare-UUID resume** (`identity/handlers.py`, `middleware/identity_step.py`) now requires a `continuity_token` whose signed `aid` claim matches the requested `agent_uuid`. Prior behavior accepted any known UUID as proof of ownership — the mechanism behind "another agent resurrected a dormant agent from yesterday." Invariant #4 violation closed.
>
> **Residents updated:** SDK `GovernanceAgent` and Watcher now load their saved `continuity_token` into the client before the PATH 0 resume call so they keep working in strict mode without anchor-file schema changes.

Translation: **the token is the current possession-proof mechanism** for bare-UUID resume. Before Part C, it was performative (an optional resume convenience). Part C made it the artifact that distinguishes "process that legitimately owns UUID X" from "process that merely knows UUID X exists." `identity.md:111` flags token as retire/repurpose, but was written pre-Part-C; v2 ontology does not yet account for the anti-hijack role.

**Three possible resolutions (operator decision):**

| Option | Description | Tradeoff |
|---|---|---|
| **A. Narrow, don't retire** | Short-TTL rolling token (e.g., 1h, refreshed on `identity()` calls) as possession-proof for PATH 0. Stop emitting it as a 30-day resume credential. Workspace-cache write already gone (S11). | Smallest blast radius; preserves Part-C invariant with lower surface area. **Honestly labeled:** still performative — a TTL-only mechanism does not achieve process-instance binding (see §4.1). Recommended as a *pragmatic* first step, not an ontology endpoint. |
| **A′. Narrow + PID/nonce bind** | Option A + add a process-scoping claim to the token payload: either PID+boot-ID on the issuing host, or a per-process nonce the server invalidates on restart signal. | Actually achieves "process-instance proof" in the strict sense. Larger change — token schema bump, server-side nonce tracking, restart-signal plumbing. Worth the work if the operator wants A to land with a clean ontology-compliance claim rather than a "narrower performative" label. |
| **B. Replace with R1** | ~~Wait on R1 (`verify_lineage_claim`) to mature. Substrate-earned agents and session-like agents both route through behavioral verification instead of token-match.~~ **Invalidated 2026-04-24** by R1 v3 design doc (`docs/ontology/r1-verify-lineage-claim.md`). R1 is explicitly **not authentication** and **not a security primitive** — it detects *honest over-claims* (fresh process declaring a parent_agent_id it cannot behaviorally match), not adversarial resumes (attacker presenting a legitimate UUID). R1 and the anti-hijack role are orthogonal problems. R1 does not replace continuity_token for role (3). |
| **C. Substrate-only proof** | Substrate-earned agents (Lumen, residents passing R4 three-condition) keep a token-shaped proof tied to hardware/hardcoded-UUID anchor. Session-like agents get no resume primitive at all — they re-onboard with `force_new=true, parent_agent_id=<prior>` on every process-restart. | Matches v2 ontology cleanly. Requires S8a Phase-2 (`session_like` tagging) to be fully in place so the routing is honest. Breaks the CLI's "resume from cache" UX for ad-hoc human callers. |

**Recommendation:** **A** as the immediate landing — smallest-diff path that materially reduces the token's operational surface. **A′** as the ontology-clean follow-on if the operator wants to *earn* the "intra-process-instance proof" label rather than borrow it rhetorically. Option **C** is the ontology-pure session-like endpoint but requires S8a-Phase-2 + substrate-routing work not yet shipped. **Option B is no longer available** as previously framed; R1 (see `docs/ontology/r1-verify-lineage-claim.md`) produces a plausibility score for honest lineage claims, not a replacement for the token's anti-hijack ownership proof.

**Why A rather than A′/C for the first landing:** A ships in one release cycle without regressing Part-C. A′ adds schema change + server state; C requires S8a-Phase-2. A is the only option with no hard prerequisites. A′ can ship immediately after A as a payload-schema extension; the `ownership_proof_version` field (§4.5) is designed precisely to make A → A′ a forward-compatible bump.

**What R1 *does* contribute.** When R1 ships in shadow mode, it can run alongside the narrowed token on every `parent_agent_id`-declared onboard: the token answers "does the caller hold the secret minted to this UUID?" (adversarial-resume defense), and R1 answers "does the successor's trajectory match the parent's shape?" (honest-over-claim detection). The two signals stack without conflict. This is the "R1 as complementary signal" follow-up, not a replacement path — and it is not gated by S1.

## 4. Deprecation scope under Option A

> **Status update 2026-04-25 — most of §4 is already shipped.** A post-acceptance code-review pass found that the items below labeled "must update in S1-a" were largely landed before the operator's path-A acceptance. Specifically:
>
> | Sub-item | Status |
> |---|---|
> | §4.1 TTL shrink (`_CONTINUITY_TTL = 3600`) | **Shipped** at `src/mcp_handlers/identity/session.py:32` |
> | §4.5 `ownership_proof_version` in payload | **Shipped** at `session.py:42, 48, 54` |
> | §4.3 `build_token_deprecation_block` infrastructure | **Shipped** at `session.py:59` |
> | §4.3 `log_continuity_token_deprecated_accept` audit event | **Shipped** at `src/audit_log.py:368` |
> | §4.3 onboard handler wiring (deprecation block + audit on cross-process-instance accept) | **Shipped** at `src/mcp_handlers/identity/handlers.py:1702-1720` |
>
> **Real remaining work for S1-a:**
> 1. Wire deprecation block + audit event into `handle_identity_adapter` (the `identity()` tool path) — currently only in `handle_onboard_adapter`.
> 2. Wire same into `handle_bind_session`.
> 3. Wire same into HTTP onboard direct-tool path (`src/http_api.py`).
> 4. Add clock-skew tolerance to `resolve_continuity_token` (currently zero drift accepted; the §7.2 clock-skew test requires this to exist first).
> 5. Three regression tests per §7.2 (token-expiry-mid-call, clock-skew-near-boundary, concurrent-possessor-with-expired-token).
> 6. Chronicler regression test asserting >1h-old-token resident gets force-re-onboarded correctly (the council found this gap).
> 7. PR copy must use **hygiene framing** (operator decision 2026-04-25 — see §11.1 below) and pair with **rotating `UNITARES_CONTINUITY_TOKEN_SECRET`** at ship time (collapses §7.5 grace-window concern).
>
> The §4 sub-sections below are kept as historical-design context. Treat the inline "must update" line numbers as illustrative — read the code to confirm current state.

Landing **A** means five concrete changes, none of which delete the token primitive:

### 4.1. Stop emitting as a long-TTL resume credential

- `src/mcp_handlers/identity/session.py:22` — `_CONTINUITY_TTL = 30 * 24 * 3600` drops to a short rolling window (candidate: 1 hour = `3600`; see §11.2 for threshold-justification caveat).
- Re-issue on `identity()` calls (already happens at `handlers.py:917`) is the refresh mechanism; long-running residents get rolling refresh as a side effect.
- Processes that go TTL-without-call get forced through re-onboard — ontology-compliant behavior for session-like agents, correct for Chronicler-style launchd-cron residents (see §5 breakage note).

**Honest characterization of what this buys.** A short-TTL HMAC token proves *recent-minting possession*, not *process-instance continuity*. The same token can be read from disk or shared memory by any co-resident process on the issuing host; HMAC-with-TTL cannot distinguish the minting process from any other possessor. Calling this "intra-process-instance proof" is rhetoric, not mechanism — the real ontology label is **"performative, narrowed"**. Actual process-instance binding requires A′ (PID-or-nonce claim). Operators weighing A should do so with this tradeoff explicit.

### 4.2. Reframe server response field (deferred to post-S1-c)

- Response field renamed at the API boundary: `continuity_token` → `ownership_proof` (or retained as alias during grace period with a `deprecated: true` flag in a new `identity_status.deprecations` block).
- `continuity_token_supported` field similarly renamed/aliased.
- Diagnostic surface (`continuity_token_support_status()`) renamed.

Rename is **deferred until after S1-c** to avoid simultaneously serving two field names during the grace period. See §9.

### 4.3. Emit `deprecation` warning on cross-process-instance accept

Server can cheaply distinguish "token used within its issuing process-instance's lifetime" vs. "token presented after process-instance death, which is the retired cross-process-instance resume path." The latter emits a warning in the response:

```json
"identity_status": {
  "deprecations": [{
    "field": "continuity_token",
    "severity": "warning",
    "message": "cross-process-instance resume via continuity_token is deprecated; declare lineage via parent_agent_id on force_new=true",
    "sunset": "2026-Q4"
  }]
}
```

**Telemetry coverage caveat.** The `continuity_token_deprecated_accept` audit event will capture callers that still reach the server after the TTL shrink. It **will not capture silent-break clients** — external scripts or cached tokens that simply stop working when their 30-day TTL becomes 1 hour, and whose owners do not re-check in. Grace-period warnings close the discoverable long tail, not the silent one. `identity.md:111`'s "external-client count unknown" remains partially unknown after S1 ships.

### 4.4. Plugin `onboard_helper` stops preferring token on startup

- `scripts/client/onboard_helper.py:181-184` changes: startup onboard defaults to `force_new=true` with lineage declaration, reads cached UUID for `parent_agent_id`. Token is no longer read on startup.
- Plugin check-in helper (`unitares-governance-plugin/scripts/checkin.py`) keeps passing token *during the lifetime of the process* for PATH 0 proof — that's role (3), preserved.
- `scripts/unitares` CLI: same pattern. Startup = `force_new`; in-process calls keep using the freshly-minted token.

### 4.5. Forward-compat `ownership_proof_version` field

Add an `ownership_proof_version` integer field to the token payload (and surface it in `identity_status`) set to `1` on S1-a ship. This is a **no-behavior-change schema extension** that buys three things:

1. **A → A′ is a bump, not a breaking change.** When A′ ships (PID/nonce binding), the payload version increments to `2` and clients can branch on it.
2. **A → B transition is visible in the wire format.** If R1 lands and the token becomes a behavioral-verification artifact, version `3` signals the semantic change to log consumers without requiring a field rename.
3. **"Operator muscle-memory" foreclosure of B is reduced.** The version field is a permanent reminder that this is a mechanism with successors, not a settled shape — present in the payload every single request, not just in a changelog.

Cost: one integer field + one accessor. Low.

**What this PR would NOT do:** delete `create_continuity_token`, delete `resolve_continuity_token`, change `bind_session` behavior beyond the TTL inheritance, touch `trajectory_identity.py`, implement R1 verification. Single-concern. Role (3) preserved intact — the HMAC infrastructure remains, the TTL shrinks, the issuance contract narrows, and the version field opens a forward path.

## 5. External-client migration story

Based on §1d inventory: **zero `continuity_token` references in three grep-able repos outside unitares + plugin** (`unitares-core` archived, `unitares-discord-bridge`, `anima-mcp`) as of 2026-04-24.

**Caveat on evidence tier.** This narrows `identity.md:111`'s "external-client count unknown" to "zero references in three known repos grepped today." It does not close the uncertainty. Possible silent consumers:

- Personal scripts on operator or collaborator machines
- Forked helpers that cached the old onboard_helper pattern
- Third-party clients authored against the HTTP surface
- Tokens pasted into shell history, memo files, or ChatOps archives that get reused later

Grace-period deprecation warning (§4.3) helps the *discoverable* long tail — clients that still reach the server — but cannot detect clients that simply break silently when their cached token expires at 1h instead of 30d. Operators should treat the "no external consumers" assumption as provisional throughout the grace period.

**Known consumers (all operator-controlled):**

1. **Codex plugin (`unitares-governance-plugin`).** S11 migrated the cache-write path. Option-A landing updates `unitares-governance-plugin/scripts/onboard_helper.py:181-184` + test expectations. In-process token-pass (`tests/test_checkin_helper.py:31-223`) stays valid — that's role (3).
2. **CLI `scripts/unitares`.** Update startup path; keep per-call token-pass.
3. **SDK `GovernanceAgent` + Watcher.** Already loading saved token for PATH 0. Short TTL means residents get forced into a re-onboard if they go TTL-without-call. Cadences: Vigil 30min, Sentinel continuous, Chronicler daily, Watcher event-driven, Steward 5min sync. Chronicler needs re-onboard on wake under 1h TTL (see §7 breakage note).

## 6. Grace period design

Grace period = one release cycle after Option-A ships.

- **Week 1:** ship the TTL shrink + deprecation warning + forward-compat field. Token still accepted cross-process-instance, but every such accept emits the `deprecations` warning block. Logs capture the warnings for audit.
- **Weeks 2-4:** monitor the deprecation-warning log. Count cross-process-instance token acceptances per channel/caller. If any unexpected caller appears, extend grace.
- **End of cycle:** server rejects cross-process-instance token acceptance (HTTP 401 on HTTP, error on MCP). Intra-process-instance PATH 0 still accepts.

> **Status update 2026-05-23 — S1-c shipped.** Live telemetry was re-run after
> the Hermes wrapper migration: 3 production-shaped grace-window emits, 1
> Hermes post-window emit before the fix, and 0 emits after the forced Hermes
> run. The server now rejects the retired token-as-resume surfaces:
> `onboard(continuity_token=...)`, token-only `identity(continuity_token=...)`,
> and token-only `bind_session(continuity_token=...)`. Same-live-process PATH 0
> ownership proof remains accepted as
> `identity(agent_uuid=..., continuity_token=..., resume=true)`, and explicit
> `client_session_id` binding remains accepted.

**Grace-period telemetry** lives in a new `audit_log` event type: `continuity_token_deprecated_accept` with fields `{caller_channel, caller_model_type, issued_at, accepted_at, lifetime_seconds, agent_uuid}`. That event populates a dashboard panel showing cross-instance resume attempts.

**Grace-period security posture.** During weeks 2-4 the server still accepts stale tokens. An attacker holding a 30d-TTL token issued pre-S1-a has the full grace window to use it; the "teeth" are warnings, not rejection. This is the deliberate tradeoff of a deprecation window, but operators should be explicit about it: grace period is a migration courtesy, not a security hardening window. If the concrete threat model requires earlier cutoff, grace can shorten to one week or go zero.

## 7. Known risks and breakage

### 7.1. Acceptable under Option A

- **Chronicler daily wake must re-onboard.** Acceptable under v2 — a launchd-daily process is structurally ephemeral-with-declared-lineage.
- **Ad-hoc human CLI callers lose 30-day token resume.** Acceptable; `force_new=true, parent_agent_id=<cached UUID>` from the plugin cache gives the same outcome (fresh process-instance with declared lineage).
- **External clients that cached tokens for long-running resume** break if any exist. Per §5, unknown.

### 7.2. Re-onboard race at TTL boundary

Under 1h TTL, a token may expire mid-session. The client discovers this on the next call, re-onboards with `force_new=true, parent_agent_id=<old_uuid>`. Between token expiry and the re-onboard call, what is the state of the old UUID?

- **Concurrent-possessor case:** If another process presents the now-expired token within server clock-skew tolerance, or presents a token for the same UUID minted in a different process-instance, the Part-C `aid`-match gate still fires. Behavior: old token rejected at the gate; that's correct.
- **Re-onboard-overlap case:** The client's `force_new=true` onboard mints a new UUID (lineage-declared). The old UUID becomes orphan-eligible on its usual schedule. No resurrection path exists under strict mode. Behavior: correct — but the "old UUID in a resumable state for seconds after TTL expiry" window is a new code path. Needs a regression test at S1-a ship.
- **Clock-skew at TTL boundary:** `_CONTINUITY_TTL` plus server clock drift could produce a window where the token is server-expired but client-believed-valid. Under 30d TTL, clock-skew was negligible; under 1h, a few minutes of drift is meaningful. Server-side: accept slight drift (say, 5 minutes) explicitly, or make TTL tolerance documented.

Add a test-surface entry at S1-a for each: token-expiry-mid-call; clock-skew-near-boundary; concurrent-possessor with expired token.

### 7.3. `bind_session` silent-coupling

`bind_session` shares the `resolve_continuity_token` path (`handlers.py:1066`). S1-a's TTL change **silently changes `bind_session`'s acceptance semantics** from "accept tokens up to 30d old" to "accept tokens up to 1h old." S9 owns the full `bind_session` re-scoping but has not yet scoped it.

**Two treatment options for S1-a:**

- **Let it propagate.** `bind_session` callers get the shorter TTL. Simple. Risk: surprises any client relying on long-lived `bind_session` resume.
- **Hold `bind_session` at 30d via separate constant.** `bind_session` continues to honor the old TTL until S9 decides. Adds one config constant; preserves status quo for that acceptor.

Recommendation: **let it propagate**, flag for S9, add a test asserting `bind_session` honors the new TTL. If this breaks a real caller, surface it in grace-period telemetry.

### 7.4. Token-in-logs cardinality

Even under short TTL, tokens appear in: audit logs, dashboard event stream (`event-visibility-pipeline.md`), Discord bridge transcripts, MEMORY.md snippets, `.unitares/session.json` v1 files still on disk from pre-S11 era. Shortening TTL reduces the blast radius of any one captured token, but the emit-everywhere pattern is unchanged by Option A. **An ownership proof should probably not be emitted at the same cardinality as a UUID.** Out of scope for S1-a as a fix; flag for operator awareness and future hardening (separate ticket).

### 7.5. Adversarial capture during grace

During weeks 2-4 of the grace period, an attacker with a captured 30d-TTL token issued pre-S1-a has the full window to use it. The server will emit warnings but accept. If the concrete threat model includes this case, shorten or skip the grace window.

## 8. What stays the same

- HMAC infrastructure (`_get_continuity_secret`, `UNITARES_CONTINUITY_TOKEN_SECRET` env, `scripts/ops/rotate-secrets.sh`) — unchanged.
- PATH 0 bare-UUID resume machinery — unchanged, just with a shorter-lived proof.
- `create_continuity_token` / `resolve_continuity_token` signatures — unchanged except TTL constant and new version field in payload.
- Plugin cache schema v2 — already token-free, no change.

## 9. Sequencing

Suggested commit-shaped PRs, in order:

1. **S1-a: TTL shrink + deprecation warning + forward-compat version field (`unitares`).** `_CONTINUITY_TTL = 3600`, add `continuity_token_deprecated_accept` audit event, add `identity_status.deprecations` response block, add `ownership_proof_version: 1` to payload. No rename. One PR to master; auto-merge via `ship.sh` runtime path.
2. **S1-b: onboard-helper + CLI migration (`unitares-governance-plugin`, `unitares/scripts/unitares`).** Swap startup path from cached token to `force_new + parent_agent_id`. In-process token-pass preserved. Two PRs (one per repo).
3. **S1-c (post-grace): cross-process-instance reject.** **Shipped 2026-05-23** after clean post-Hermes telemetry. Token-only resume/bind surfaces now return `status=continuity_token_resume_rejected`; PATH 0 ownership proof and explicit `client_session_id` binding are preserved.
4. **S1-d (optional, deferred): field rename** `continuity_token` → `ownership_proof`. Only after S1-c settles.
5. **A′ (optional, pre-B): PID/nonce binding.** Schema bump to `ownership_proof_version: 2`. Ships if operator wants the real "intra-process-instance proof" claim.

S1-a alone closes most of the ontology-hygiene concern without touching client code. Can ship independently. S1-b is the follow-on that aligns client behavior. S1-c is operator-gated on telemetry. S1-d and A′ are optional.

## 10. What this plan does NOT decide

- **A / A′ / B / C choice.** Recommendation is A → A′ (forward-compat field positions A to bump to A′). Operator approval required before S1-a ships.
- **TTL value.** 1 hour is a candidate; 30 or 15 minutes would be more aggressive. See §11.2 — threshold-justification is thin.
- **Sunset date on the deprecation warning.** "2026-Q4" is a placeholder.
- **`bind_session` treatment.** See §7.3. Recommendation is let-it-propagate; operator can override.
- **R1 replacement.** ~~Option B is the long-term endpoint; S1 intentionally does not block on it.~~ **Withdrawn 2026-04-24** — R1 v3 scope is plausibility scoring, not ownership proof. See updated §3 Option B row.
- **Whether Option A crosses into "earned continuity" territory.** §4.1 argues it does not — A is "performative, narrowed", not earned. Operator may disagree; if so, name the disagreement explicitly rather than implicitly.
- **Grace-period length and security posture.** §6 suggests one release cycle with warning-only. If threat model requires faster cutoff, shorten.

## 11. Operator decision points

> **All eight decisions resolved 2026-04-25.** Outcomes recorded inline below. The "Recommend" lines remain as historical context for the reasoning.

1. **A / A′ / C? — A → A′ accepted.** Recommend A → A′. Option B withdrawn 2026-04-24 per R1 v3 scope clarification.
2. **TTL target for Option A? — 1h accepted.** Recommend 1h. **Justification caveat:** 1h is a convenience anchor (fits resident cadences in §5), not a threat-model anchor. The relevant question is attacker-window, not client-cadence — "how long would a stolen token need to be useful to be dangerous?" is not the same as "how often does Vigil wake." If the operator has a threat-model-derived budget (e.g., "tokens exfiltrated from logs should expire before a human operator could notice"), that number should drive the TTL. **Operator picked 1h under hygiene framing** — long enough that all rolling-refresh resident cadences stay covered, short enough to not claim "long-lived credential," not so short (5min) that clock-skew false positives outweigh proportional security gain. **Effective resolve window post-S1-a (2026-04-29) is TTL + 30s** under `_CLOCK_SKEW_TOLERANCE` — security teeth come from rotation (§7.5 / decision #7), not TTL math, so the 30s NTP-drift grace does not contradict the 1h convenience anchor. Bounded below the 60s minimum TTL so it cannot swallow whole-token validity. Documented at the constant definition in `src/mcp_handlers/identity/session.py`.
3. **Grace-period length? — one release cycle, warning-only, accepted.** Recommend one release cycle (effectively "until telemetry is clean"). See §6 security-posture note. Tightening explicitly addressed via decision #7 below (secret rotation).
4. **Field rename (`continuity_token` → `ownership_proof`)? — deferred to S1-d.** Confirmed; S1-d post-grace.
5. **Chronicler re-onboard-on-wake acceptable? — accepted.** Correct v2 behavior for launchd-daily processes.
6. **`bind_session` TTL: let-propagate or hold? — let-propagate accepted.** Coupling parked under S9 in `plan.md` with a regression-test requirement; not a silent deferral.
7. **Grace-period security posture: warning-only or early cutoff? — warning-only + secret rotation accepted.** Operator paired the warning-only grace with **rotating `UNITARES_CONTINUITY_TOKEN_SECRET` at S1-a ship**. Rotation invalidates all pre-S1-a 30d-TTL HMACs in one stroke, collapsing the §7.5 "attacker with stolen pre-S1-a token" window without requiring a philosophical claim about TTL math being security teeth. This is the cheap-and-honest path: hygiene framing in PR copy + actual security move via secret rotation.
8. **Is "performative, narrowed" an acceptable ontology label? — accepted under hygiene framing.** §4.1's honest label stands. A is hygiene with a security-side-effect; the operator did not pick the security-claim framing. A→A′ remains the explicit security-upgrade path if PID/nonce binding is later wanted.

**Implementation directive for next process-instance:** PR title and body for S1-a use hygiene language. Example: "narrow continuity_token's role to within-process-instance use; preserve PATH 0 anti-hijack with shorter-lived possession proof." Do **not** write "tighten ownership-proof TTL to reduce window of stolen-token replay" — that silently renegotiates §4.1's label into a security claim the operator did not pick.

## 12. Pointers for next process-instance executing this plan

- Read this doc + `docs/ontology/identity.md` §Implications + `docs/CHANGELOG.md:35-43` (Part C context) first.
- Check `plan.md` S1 row for `WIP-PR:` field before opening S1-a.
- Target: `unitares` master. Docs-only preflight (this doc) is already on master; S1-a is the first code PR.
- Cite this doc's §4 and §7 in the S1-a PR body. §7 in particular surfaces three risks that need regression tests.
- Verify S1-a behavior does NOT break Part-C's anti-hijack invariant — regression test for PATH 0 with expired token must reject; PATH 0 with in-TTL token must accept.

---

## Appendix: Call-site quick reference

> **2026-04-25 council audit refreshed the line numbers below.** Many citations in the 2026-04-24 draft were off by 20-60 lines because the implementation landed in parallel with doc-writing. Treat the line numbers as guidance; the implementer should grep for the symbol name to locate the current site.

**Emit sites — already shipped (do NOT re-edit in S1-a):**
- `src/mcp_handlers/identity/session.py:32` — `_CONTINUITY_TTL = 3600` (1h)
- `src/mcp_handlers/identity/session.py:42, 48, 54` — `_OWNERSHIP_PROOF_VERSION` injected into payload
- `src/mcp_handlers/identity/session.py:59` — `build_token_deprecation_block` (deprecation-block infrastructure)
- `src/mcp_handlers/identity/session.py:120` — `create_continuity_token` (signature already accepts `ttl_seconds` parameter for tests)
- `src/audit_log.py:368` — `log_continuity_token_deprecated_accept`
- `src/mcp_handlers/identity/handlers.py:1702-1720` — onboard-handler wiring of deprecation block + audit event on cross-process-instance accept

**Accept sites — wired 2026-04-29 in S1-a (single-PR-strategy):**
- `handle_onboard_v2` — refactored to use shared `_emit_continuity_token_deprecation` helper (was inline)
- `handle_identity_adapter` — wired via shared helper, fires when `continuity_token` present and `not force_new`
- `handle_bind_session` — wired via shared helper, fires only when token was used to derive `client_session_id` (i.e., caller passed token without an explicit session id). Pure in-session bridges (caller passes `client_session_id` directly) do NOT fire — that's the legitimate intra-session use.
- `src/http_api.py` `/v1/tools/call` — HTTP REST inherits transitively through `execute_http_tool` dispatch into the same handlers; no separate wiring needed.

**Code surfaces changed in S1-a (2026-04-29):**
- `_CLOCK_SKEW_TOLERANCE = 30` (`src/mcp_handlers/identity/session.py`) — 30s NTP-drift grace, bounded below the 60s minimum TTL so it cannot swallow whole-token validity.
- `resolve_continuity_token` — expiry check now `exp + _CLOCK_SKEW_TOLERANCE < now` (was `exp < now`).
- `_emit_continuity_token_deprecation` helper in `handlers.py` — single source of truth for deprecation-block + audit-event emission across the three accept sites.
- Existing `test_resolve_rejects_expired_token_at_ttl_boundary` test renamed/rewritten to `test_resolve_rejects_token_past_clock_skew_window` reflecting the new boundary semantics. The test's original comment ("any loosening must be deliberate") was honored — this is the deliberate loosening.

**Client sites (S1-b, separate PR):**
- `scripts/unitares` — cache read + pass paths (verify line numbers; doc previously cited :70, :148)
- `scripts/client/onboard_helper.py` — startup preference (verify)
- Plugin mirror of `onboard_helper.py` (separate plugin-repo PR)

**Test surfaces that will need updating / adding (S1-a regression coverage):**
- `tests/test_identity_session.py` — `test_priority_1_continuity_token*` (intra-process still works; add TTL-boundary + clock-skew assertions)
- `tests/test_identity_payloads.py` — payload shape (`deprecations` block + `ownership_proof_version`)
- `tests/test_name_cosmetic_invariant.py` — banner-text assertion may need update
- **New regression tests required:**
  - PATH-0-with-expired-token reject (§7.2 token-expiry-mid-call)
  - Clock-skew-near-boundary (requires implementing tolerance first)
  - Concurrent-possessor with expired token (§7.2)
  - `bind_session` with short-TTL token (§7.3 — currently no test asserts `bind_session` honors any specific TTL)
  - **Resident re-onboard regression:** assert that a resident with a >1h-old token correctly forces through re-onboard rather than erroring out. Council found this is a greenfield test gap; Chronicler is the structural example.

**Operational steps at S1-a ship (paired with code PR):**
- **Rotate `UNITARES_CONTINUITY_TOKEN_SECRET`** (operator action — see decision #7). Triggers re-mint of all in-flight tokens; pre-rotation 30d-TTL HMACs become unverifiable.
- Restart `com.unitares.governance-mcp` LaunchAgent to pick up the rotated secret.
- Note in PR body that pre-S1-a 30d tokens are invalidated by rotation; this is the operational form of §7.5's "early cutoff" without philosophical TTL claims.
