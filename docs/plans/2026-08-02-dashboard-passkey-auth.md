# Dashboard passkey sign-in (WebAuthn) — design & handoff plan

**Status:** Design draft, pending operator approval. Planning and implementation are separate sessions.
**Surface:** `unitares` repo — `src/http_api.py`, `dashboard/redesign/`, one MANUAL migration. **Auth surface ⇒ draft PR, operator merge gate, never auto-merge.**

> Several requirements below are counter-intuitive and were derived from tracing the live auth paths. Each carries its rationale inline (**Why:**). Do not simplify one away without refuting its stated reason.

---

## 1. Problem, goals, non-goals

**Problem.** Browser access through `gov.cirwel.org` needs two tokens (`?token=` API bearer living in the bookmark URL; `?operator_token=` absorbed to localStorage). Mobile browsers evict localStorage; URLs leak (history sync, screenshots, CF edge). Every device needs manual provisioning. 2026-08-01: the operator's phone retry-looped 403 on `/ws/eisv` for an hour after PR #1447 correctly gated that socket.

**Goals.** Standard passkey sign-in (Face ID, one tap) · device-agnostic (iCloud sync + QR cross-device for borrowed browsers) · **no new third party** (ceremonies + sessions on the origin; CF's view unchanged; no IdP) · replaces both tokens for browsers · `/ws/eisv` authenticates by the same session · **purely additive** (zero enrolled credentials ⇒ deploy is a no-op).

**Non-goals.** No changes to `/mcp/`, `/sse`, MCP transport security, or the claude.ai connector. Programmatic clients keep the bearer. Single operator (schema allows more, UI doesn't). `lumen.cirwel.org` out of scope.

## 2. Live-state facts (verified 2026-08-01; RE-VERIFY before building — line numbers drift)

The implementing session cannot see the planning conversation. This section is what you cannot infer from the repo alone.

- **⚠️ BRANCH FROM `origin/master`, NOT the dev checkout's HEAD.** `~/projects/unitares` currently sits on `codex/aucdelta` (`018b5c15`), which contains **zero** occurrences of `_check_ws_auth` — PR #1447's websocket gate does not exist there. `origin/master` has it. **Building from that stale tree and merging would revert the gate and restore the leak** (unauthenticated 101 streaming agent ids, EISV, verdicts, Lumen sensor payload). First command: branch from `origin/master`; then `grep -c _check_ws_auth src/http_api.py` and refuse to proceed on 0. Work in a fresh worktree (repo convention), never in the shared dev checkout.
- **REST auth is CENTRALIZED: one function, ~33 callers.** `_check_http_auth(request, ...)` is called from ~33 routes; the WS equivalent `_check_ws_auth` (≈202-238) is called once at ≈4189. **Implement session acceptance INSIDE those two function bodies — do not touch call sites.** ⚠️ `http_api.py:~1127` is inside `http_health`, public BY DESIGN (reads the token env only to report `auth.enabled`) — never gate it.
- **Both gates fork on posture at the top:** `if mcp_bearer_required(): return check_mcp_bearer(...)` — hosted posture, deliberately no IP bypass. **Live-verified: `UNITARES_MCP_BEARER_TOKENS` is UNSET, `UNITARES_HTTP_API_TOKEN` is SET**, so the local branch is what runs. Put the session check in the **local branch**, and add an explicit comment that cookie auth is NOT authorized into the hosted branch without an operator decision (that branch exists to admit bearers only).
- **Both gates currently FAIL OPEN on an unset token** (`if not http_api_token: return True`). Since this design's endgame is "stop needing the token," that is a landmine: unsetting it would open the whole REST surface and `/ws/eisv` to the internet silently. **PR-A must flip both to fail-closed** and add a test.
- **`_is_trusted_network` (≈166)** returns True for loopback/Tailscale; uvicorn proxy-headers rewrites `request.client.host` from `X-Forwarded-For`, so tunnel traffic is untrusted — that's why the phone needed tokens. **Live-verified both directions:** loopback `/v1/residents` → 200 with no bearer; same + `X-Forwarded-For: 8.8.8.8` → 401. **Spoof-resistance verified externally:** through the tunnel, `X-Forwarded-For:` of `127.0.0.1`, `::1`, and the tailnet IP all → **401**. Client-supplied XFF does not win. Keep this bypass untouched.
- **Operator write path.** REST write routes gate on the BOOLEAN `is_operator_caller(signals)` — `/v1/sentinel/adjudicate` registered ≈4337, gate ≈3313-3317; `/v1/harness/outcome` identical. The persisted-identity resolver `resolve_operator_identity` (`src/mcp_handlers/identity/operator.py`) is used by the **MCP dispatch path, not these REST routes** — so today an adjudication attributes to Sentinel's own UUID and there is **no per-operator identity to match against**. See D8 for what this forces.
- **`websocket.cookies` is inherited** from `starlette.requests.HTTPConnection` (starlette 0.52.1, `requests.py:149`), not defined on `WebSocket`. Grepping `websocket.py` and finding nothing is expected.
- **Tunnel topology:** cloudflared (ingress remote-managed in the CF dashboard, not a local config.yml) → `[::1]:8767` via `ipv6_loopback_proxy.py` (deliberate infra — do not "fix") → server binds `0.0.0.0:8767`. **TLS terminates at CF; the origin sees plain HTTP + `X-Forwarded-*`.** Cookies may be `Secure` (browser↔CF is HTTPS), but never require `request.url.scheme == "https"` server-side.
- **Host allowlist (#1413) LIVE** — 421 on unknown Host. `gov.cirwel.org` allowlisted; this design adds no hostname.
- **Migrations MANUAL.** Live-verified: DB max applied = **56**, disk max = `056_…`, no `057_*` — 057 is free. **Every recent migration self-registers**; 057 must end with `INSERT INTO core.schema_migrations (version, name, applied_at) VALUES (57,'dashboard_webauthn',NOW()) ON CONFLICT (version) DO NOTHING;` or the operator's per-deploy `schema_migrations` diff won't show it.
- **`cryptography` is already a DIRECT dep** in `[project.optional-dependencies].full` (pyproject ≈43) alongside starlette/uvicorn — not transitive; nothing to hunt for.
- **No `/auth` prefix exists** anywhere in `http_api.py` and no catch-all shadows it — the 8 routes append cleanly in `register_http_routes` (≈4261, appends ≈4304-4359).
- **Repo rules:** draft PR · `--no-metered-API` (no ANTHROPIC_API_KEY in CI) · tests green before handback · merge ≠ deploy (separate operator step).

## 3. Design decisions (decision → rationale → rejected alternative)

**D1. Library `py_webauthn` (`webauthn` ≥2.x, BSD).** Maintained, pure-python atop `cryptography` (already present). Rejected: hand-rolled CBOR/COSE (auth is the worst place for NIH); `fido2` (heavier for server-only).

**D2. `rp_id = "gov.cirwel.org"` exact.** Rejected zone-wide `cirwel.org` — would let any subdomain assert the credential. **Pitfall:** a passkey enrolled on `localhost` will NEVER work on `gov.cirwel.org`; enrollment must happen on the tunnel origin. Local tests use `localhost` fixtures.

**D2b. `expected_origin` is a HARDCODED constant `https://gov.cirwel.org`** (or env), asserted in a test. **Never derive it from the request** — the origin sees plain HTTP, so `f"{request.url.scheme}://{host}"` yields `http://…` and fails every verification, and the tempting "fix" is to trust attacker-influenceable `Host`/`X-Forwarded-Proto`. Pin `rp_id` and `expected_origin` as a matched pair.

**D3. Enrollment bootstrap = operator token in a HEADER or a single-use short-TTL code — NEVER a query string.** The original "open the provisioning URL on the phone" laundered the exact token-in-URL weakness this design exists to kill, into the *same iCloud account* that syncs the passkey (one compromise ⇒ credential + enrollment key). Bootstrap options: (a) enroll from the Mac on loopback where `_is_trusted_network` already trusts you — but see D2's rp_id pitfall, so this needs the tunnel origin in the browser; **(b) preferred: an operator-authenticated POST mints a 10-minute single-use enrollment code, which the operator types on the phone.** Every `webauthn_enrolled` event fires an out-of-band notification (Discord bridge) — silent enrollment must be impossible.

**D4. Server-side sessions in Postgres, opaque cookie.** Store `sha256(session_id)`, compare by hash. **Cookie name `__Host-unitares_session`** — the `__Host-` prefix is browser-enforced Secure + `Path=/` + no `Domain`, which makes sibling-subdomain cookie-tossing (`cirwel.org`, `lumen.cirwel.org` are same-site!) unrepresentable. `HttpOnly; Secure; SameSite=Lax; Max-Age` 30d sliding, 90d hard cap. Rejected: JWT (no revocation without a denylist = a session store anyway); Redis-only (documented wipe-incident class here).

**D5. Precedence — ADDITIVE acceptance, with the fail-open closed.** Read: trusted-network → bearer → **session** → 401. WS: query token → header → **session cookie (+ Origin check, D6b)** → trusted-network → 403. Write: `X-Unitares-Operator` header → **session** → 403. **Explicit branch: token unset ⇒ DENY** (was: allow). Zero enrolled credentials ⇒ no behavioral change.

**D6. CSRF on writes:** `SameSite=Lax` + required `X-Unitares-Csrf: 1` header (non-simple header ⇒ preflight; cross-origin pages can't add it). Assert `allow_credentials` stays False on the existing `CORSMiddleware` (`src/mcp_server.py` ≈452-469).

**D6b. WebSocket Origin check — the CSRF header does NOT protect `/ws/eisv`.** WebSocket handshakes bypass CORS entirely, and `cirwel.org`/`lumen.cirwel.org` are same-site for cookie purposes, so a page on either could open `wss://gov.cirwel.org/ws/eisv` and ride the cookie. **When authenticating a socket by cookie, require `Origin == https://gov.cirwel.org` exactly.** Token-authenticated sockets keep working as today.

**D7. Ceremony parameters.** `user_verification="required"`; **`residentKey="required"`** (discoverable credentials are what make one-button usernameless sign-in work); `attestation="none"`; challenge 32 random bytes, single-use, 120s TTL. **`/auth/webauthn/options` returns an EMPTY `allowCredentials`** — returning real ones would make an unauthenticated internet-facing enumeration oracle for the operator's credential IDs. Store and check `sign_count` monotonicity only when nonzero (iCloud-synced passkeys report 0).

**D8. Session ↔ operator identity — scope honestly.** The original claim ("session resolves to the same operator identity; verify in `audit.events`") had no target: REST writes only check a boolean today. **Choose ONE and write it in the PR description:** (a) *preferred, small* — session grants the same boolean authorization, and §7 verifies pass/fail parity, not identity continuity; or (b) *larger, separate PR* — route `/v1/sentinel/adjudicate` through `resolve_operator_identity` so a real per-operator identity exists to compare. **Do not silently do (b) inside PR-A.** Audit events (`dashboard_signin`, `webauthn_enrolled`, `dashboard_session_revoked`, `webauthn_credential_revoked`) carry the operator label either way.

**D9. Credential management requires MORE than a session.** A bare session cookie must NOT be able to enroll a new authenticator or revoke the last credential — otherwise one stolen cookie mints an attacker credential that survives every token rotation and locks the operator out, a capability no existing path has. **Enrollment and last-credential revocation require a fresh operator-token presentation or step-up re-auth with an existing passkey inside a short window.** Ship an active-sessions list with revoke-all (the `user_agent` column exists for this; QR/hybrid flows leave 30-day sessions on borrowed machines).

**D9b. Revocation must cascade.** `credential_id` is `NOT NULL`; session validation JOINs `webauthn_credentials` and rejects when `revoked_at IS NOT NULL`; the revoke handler also `UPDATE core.dashboard_sessions SET revoked_at=now() WHERE credential_id=$1`. Without this, "phone stolen → hit revoke" leaves the thief's session sliding for 30 days.

**D10. Front-end, small and degrading cleanly.** `data.js`: on 401 from `authFetch` with no stored token → redirect `/auth/signin`. **`adjudicate()` (≈416-428) does NOT use `authFetch`** — it builds headers and calls `fetch` directly, so PR-B must touch **two** call sites. **`ws.js` cannot branch on 403** — browsers don't expose failed-handshake status to JS (you see a generic close/1006). Rule: *try cookie-implicit connect first; on any early close/error, retry once with `?token=` if a token exists.* Vanilla JS, matching the existing section-module pattern.

## 4. Schema — MANUAL migration 057 (idempotent, self-registering)

```sql
-- 057_dashboard_webauthn.sql — MANUAL migration, do NOT auto-run
CREATE TABLE IF NOT EXISTS core.webauthn_credentials (
 credential_id BYTEA PRIMARY KEY,
 public_key BYTEA NOT NULL,
 user_handle BYTEA NOT NULL, -- required for discoverable creds
 sign_count BIGINT NOT NULL DEFAULT 0,
 transports TEXT[],
 operator_label TEXT NOT NULL,
 nickname TEXT,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 last_used_at TIMESTAMPTZ,
 revoked_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_webauthn_user_handle
 ON core.webauthn_credentials (user_handle) WHERE revoked_at IS NULL;

CREATE TABLE IF NOT EXISTS core.dashboard_sessions (
 session_hash BYTEA PRIMARY KEY,
 credential_id BYTEA NOT NULL REFERENCES core.webauthn_credentials(credential_id),
 operator_label TEXT NOT NULL,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 expires_at TIMESTAMPTZ NOT NULL,
 hard_expires_at TIMESTAMPTZ NOT NULL,
 last_seen_at TIMESTAMPTZ,
 revoked_at TIMESTAMPTZ,
 user_agent TEXT
);
CREATE INDEX IF NOT EXISTS idx_dashboard_sessions_expiry
 ON core.dashboard_sessions (expires_at) WHERE revoked_at IS NULL;

-- challenge store — was asserted in prose but never created
CREATE TABLE IF NOT EXISTS core.webauthn_challenges (
 pre_session_hash BYTEA PRIMARY KEY,
 challenge BYTEA NOT NULL,
 ceremony TEXT NOT NULL, -- 'register' | 'authenticate'
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 expires_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_webauthn_challenges_expiry
 ON core.webauthn_challenges (expires_at);

-- enrollment codes (D3) — single-use, short TTL
CREATE TABLE IF NOT EXISTS core.webauthn_enroll_codes (
 code_hash BYTEA PRIMARY KEY,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 expires_at TIMESTAMPTZ NOT NULL,
 used_at TIMESTAMPTZ
);

INSERT INTO core.schema_migrations (version, name, applied_at)
VALUES (57, 'dashboard_webauthn', NOW())
ON CONFLICT (version) DO NOTHING;
``` **Challenge handling:** one row per pre-session cookie (upsert), consumed atomically via `DELETE … RETURNING` **before** verification, `ceremony` discriminator checked so a registration challenge can't satisfy an authentication verify. **Sweep expired rows on every challenge WRITE, not on sign-in** — `/auth/webauthn/options` is unauthenticated, so a GC gated on the success path is a GC gated on the thing being flooded. Rate-limit that route per IP.

## 5. Endpoints

| Route | Method | Gate | Purpose |
|---|---|---|---|
| `/auth/signin` | GET | none (inert with no credentials) | sign-in page |
| `/auth/webauthn/options` | POST | none, **rate-limited**, empty `allowCredentials` | assertion options + challenge |
| `/auth/webauthn/verify` | POST | challenge (single-use) | verify → set `__Host-` cookie |
| `/auth/enroll` | GET | **operator token header OR enroll code** (D3) | enrollment / management page |
| `/auth/webauthn/register/options` | POST | same as `/auth/enroll` | registration options |
| `/auth/webauthn/register/verify` | POST | same + challenge | store credential; notify out-of-band |
| `/auth/logout` | POST | session + CSRF header | revoke this session |
| `/auth/sessions` | GET/POST | session + CSRF | list / revoke-all (D9) |
| `/auth/credentials/<id>/revoke` | POST | **step-up** for last credential (D9) | revoke + cascade (D9b) |

Appended in `register_http_routes`; no new listener, no Host-allowlist change.

## 6. Phasing (2 draft PRs)

**PR-A (server):** migration 057 · ceremonies · session helper · **fail-open→fail-closed ** · gate extensions inside `_check_http_auth`/`_check_ws_auth` local branch · WS Origin check · audit events · tests. Inert with zero credentials.
**PR-B (dashboard):** sign-in page · enroll/manage UI · `data.js` (two call sites ) · `ws.js` retry rule . Depends on PR-A.

**Deploy runbook (operator-visible, after merge):**
1. `psql -d governance -f db/postgres/migrations/057_dashboard_webauthn.sql` — MANUAL; preflight is blind to it. Verify `to_regclass('core.webauthn_credentials')` and that `schema_migrations` now shows 57.
2. `scripts/ops/deploy-mcp.sh` from `~/projects/unitares-deploy`.
3. Mint an enrollment code; enroll from the phone on `https://gov.cirwel.org/auth/enroll`.
4. Run §7. No plist changes; no new env required.

## 7. Tests + post-deploy live verification

**CI:** ceremony round-trips (rp_id `localhost` fixtures) · challenge single-use/TTL/ceremony-mismatch · cookie flags exact incl. `__Host-` · session sliding + hard cap + revocation + **credential-revoke cascade ** · **token-unset ⇒ DENY ** · WS accepts cookie / rejects foreign Origin / still accepts `?token=` · `/health` still public · write path requires CSRF header · D5 precedence table case-by-case · **a test that fails if `/ws/eisv` has no pre-`accept()` gate ** · `allow_credentials` stays False · zero-credential deploy leaves every existing test green.

**Post-deploy live (behavior, not ancestry):**
- Phone through tunnel, no tokens: sign in with Face ID → dashboard loads → `/ws/eisv` streams (403-loop class dead).
- Adjudication POST from that session succeeds; verify per D8's chosen scope.
- Unauthenticated `curl` from a non-trusted address: 401/403 as before. Re-run the XFF spoof set (`127.0.0.1`, `::1`, tailnet IP) → all 401.
- Existing bearer + operator-token paths still work (break-glass intact).
- Fleet check-ins unaffected across the restart (~5 min watch).

## 8. Open operator decisions (defaults chosen; veto before implementation)

1. **rp_id exact host** (widening later = one re-enroll tap).
2. **Session 30d sliding / 90d hard cap.**
3. **Keep `?token=` break-glass indefinitely** — just stop needing it. **Note : "retire the token" now means DELETING the env var, which after the fail-closed fix means locking out non-session clients — a deliberate act, never a cleanup.**
4. **D8 scope (a) boolean parity vs (b) real operator identity on REST writes.** Default (a).

## 9. Handoff notes for the implementing session

- Read this doc top to bottom. **§2 first — especially : branch from `origin/master` and confirm `_check_ws_auth` exists before writing code.** The shared dev checkout is on a stale branch missing PR #1447; building there would revert a live security gate.
- Work in a fresh worktree, never the shared checkout. Draft PRs only; auth surface ⇒ operator merges.
- Re-verify §2 line numbers against your branch — they drift.
- Don't touch `/mcp/` transport security, `ipv6_loopback_proxy`, or `http_health`.
- Migration is MANUAL, number 057, must self-register in `core.schema_migrations`.
- If you make governance MCP calls: onboard fresh with `spawn_reason="handoff"`; never resume a UUID found in workspace files.
- When PR-A/PR-B are open, report the numbers back to the operator. Merge ≠ deploy in this repo.
