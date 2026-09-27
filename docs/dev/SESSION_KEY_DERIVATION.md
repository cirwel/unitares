# Session-key derivation — schemes, stability, and privacy/security model

How the server decides "which caller is this?" on each request, and what each
signal is and isn't trusted for. The mechanics live in one function path:
`derive_session_key` / `_derive_session_key_impl` in
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
gate behind "strict identity is a write gate" (see CLAUDE.md): **writes require
a caller-asserted binding; reads may run without one, but a pre-onboard read
never shows a server-inferred binding's state.** A pre-onboard read
(`check_working_state`, `get_governance_metrics`, a knowledge search) resolves
an identity only on proof the caller sent with the call: its own
`client_session_id`, a verified `continuity_token`, or an `X-Session-ID` header
that won the derivation. Anything else leaves it unbound, on both transports:
the `/mcp/` identity step short-circuits it before resolution
(`middleware/identity_step.resolve_identity`, #945), and the REST prebind
applies the same gate (`http_routes/access._resolve_http_session_binding`).
Neither consults the sticky transport binding for such a read, and an
`agent_uuid` argument or a UUID `X-Agent-Id` header is not read proof, since the
derivation ignores both.

- **`caller_asserted`** — the caller *transmitted a proof in this request*:
  `continuity_token`, `mcp_session_id`, `x_session_id`, `oauth_client_id`,
  `x_client_id` (the `_CALLER_ASSERTED_SOURCES` set), plus an explicit
  `client_session_id` **only if it was not transport-injected**.
- **`server_inferred`** — everything the server *derived* rather than received:
  the IP/UA fingerprint, the pin lookup, the contextvar/stdio fallbacks, an
  invalid token, and a `client_session_id` the transport injected on the
  caller's behalf. These can resolve a session for a write, but must not
  satisfy strict for one (a substrate-earned agent aside), and they never
  answer a pre-onboard read.

Why the injected-CSID carve-out matters: on REST
(`http_routes/tools._inject_http_client_session`), a body with no
`client_session_id` key gets one the transport derives from the request's own
signals. If that injected value is the IP/UA fingerprint, treating it as
caller-asserted would let network-shared callers write under each other's
identity — hence injected ⇒ `server_inferred`. `/mcp/` injects nothing: FastMCP
None-fills a declared `client_session_id` before the typed wrapper runs, and
the nested `use_tool` path stopped copying the transport session in #2478, so an
omitted id is derived from the transport signals themselves. On a real
`/mcp/` call the flag is set only by the pre-mint recovery of a required call,
which re-threads the id a recovered identity was onboarded under (the typed
wrapper's own inject branch is unreachable there).

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

`derive_session_key` picks the strongest *present* signal; `proof_origin`
then decides whether that signal is strong enough to **write**. Writes require
a proof the caller actually transmitted; a pre-onboard read without one runs
unbound rather than showing an inferred binding's state.
