# Session-key derivation — schemes, stability, and privacy/security model

How the server decides "which caller is this?" on each request, and what each
signal is and isn't trusted for. The mechanics live in one function —
`derive_session_key()` / `_derive_session_key_impl()` in
`src/mcp_handlers/identity/session.py` — fed by a frozen `SessionSignals`
snapshot captured once at the transport layer (`src/mcp_handlers/context.py`).
This doc is the privacy/security companion to that code and to the identity
ontology (`docs/ontology/identity.md`).

There are several session/identity signals, not because the model is
fragmented, but because different transports expose different proofs. They are
read in a **single** priority order by **one** function, then classified on a
second axis — proof origin — that decides what the session may *do*.

## Axis 1 — resolution priority (which signal wins)

Highest to lowest (`_derive_session_key_impl` docstring is the source of truth):

| # | Signal | Source | Stability |
|---|--------|--------|-----------|
| 1 | `continuity_token` | signed resume token in call args (verified) | strong, explicit |
| 2 | `client_session_id` | explicit caller-provided proof string | strong, explicit |
| 3 | `mcp_session_id` | MCP protocol `mcp-session-id` header | stable per protocol session |
| 4 | `x_session_id` | `X-Session-ID` HTTP header | stable, client-chosen |
| 5 | `oauth_client_id` | `oauth:CLIENT_ID` from the Bearer token | stable per OAuth client |
| 6 | `x_client_id` | `X-Client-Id` / `X-MCP-Client-Id` header | stable-ish |
| 7 | `ip_ua_fingerprint` + pin | `IP:MD5(UA)[:6]`, then a Redis onboard-pin lookup | **unstable** — derived, needs a pin |
| 8 | contextvars fallback | ambient request context | backward-compat only |
| 9 | stdio fallback | single-user transports (Claude Desktop) | single-user |

The first signal present wins; lower tiers are only consulted when the higher
ones are absent.

## Axis 2 — proof origin (what the session may DO)

Resolving a key is not the same as trusting it. Each resolution is tagged
`caller_asserted` or `server_inferred` (`set_session_proof_origin`). This is the
gate behind "strict identity is a write gate" (see CLAUDE.md): **reads may work
for a server-inferred caller; writes require a caller-asserted one.**

- **`caller_asserted`** — the caller *transmitted a proof in this request*:
  `continuity_token`, `mcp_session_id`, `x_session_id`, `oauth_client_id`,
  `x_client_id` (the `_CALLER_ASSERTED_SOURCES` set), plus an explicit
  `client_session_id` **only if it was not transport-injected**.
- **`server_inferred`** — everything the server *derived* rather than received:
  the IP/UA fingerprint, the pin lookup, the contextvar/stdio fallbacks, an
  invalid token, and a `client_session_id` the transport injected on the
  caller's behalf. These resolve a session for read continuity but must not
  satisfy strict for a write.

Why the injected-CSID carve-out matters: a remote connector that only sends
schema-advertised params won't send `client_session_id`, so the server injects
one from context. If that injected value is the IP/UA fingerprint, treating it
as caller-asserted would let network-shared callers write under each other's
identity — hence injected ⇒ `server_inferred`.

## Privacy notes

- **`ip_ua_fingerprint` is network-derived**, not a user identifier: `IP` joined
  to a truncated `MD5(User-Agent)`. It is a **last-resort** signal and is
  deliberately weak. Two distinct agents behind the same gateway IP **and** the
  same User-Agent collapse onto one fingerprint — which is exactly why a
  well-behaved client should send its own `client_session_id` (tier 2) so its
  attribution stays isolated, and why the fingerprint cannot satisfy a write.
- The fingerprint truncates the UA hash (`[:6]`) — enough to disambiguate, not a
  durable cross-request identifier; it pairs with a short-TTL Redis onboard pin
  rather than being persisted as identity.
- **`oauth_client_id`** identifies an OAuth *client*, not an end user.
- `peer_pid` (UDS only, kernel-attested) and `unitares_operator_token`
  (operator-tier bearer) are **separate** trust signals, not transport
  fingerprints — see `src/substrate/verification.py` and
  `src/mcp_handlers/identity/operator.py`. Do not conflate them with session
  keys.

## One-line summary

`derive_session_key()` picks the strongest *present* signal; `proof_origin`
then decides whether that signal is strong enough to **write**. Reads are
lenient; writes require a proof the caller actually transmitted.
