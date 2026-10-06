- **Strict identity is now the default.** `STRICT_IDENTITY_REQUIRED` and `UNITARES_IDENTITY_STRICT` were opt-in. Unset, they meant that on a fresh install:
  - a bare `identity(agent_uuid=..., resume=true)` with no continuity token resumed that agent, logging a warning, and returned its session id and a new token;
  - a write that resolved no identity minted one instead of being refused.

  Unset now means strict:
  - a bare UUID resume is refused unless it carries a continuity token bound to that UUID;
  - an unresolved write gets the typed `identity_required` refusal, not a fresh identity.

  `STRICT_IDENTITY_REQUIRED` now disables strict identity only for an explicit `0`, `false`, `no` or `off`; any other value, a typo included, keeps it on. An invalid `UNITARES_IDENTITY_STRICT` value now falls back to `strict`.

  Clients that onboard with `start_session(force_new=true)` and pass back the returned `client_session_id`, as the SDK, the governance plugin and the Hermes adapter do, are unaffected. A client that relied on the permissive behavior can restore it with `STRICT_IDENTITY_REQUIRED=false` and `UNITARES_IDENTITY_STRICT=log`. This completes the staged rollout that ran local, then residents, then dispatch, before this default flip.
