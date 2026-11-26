# Identity, Lineage, Rebind, and Recovery Mechanics

Reference companion to `skills/governance-lifecycle/SKILL.md`. Load when hitting
an identity or lineage refusal, `rebind_refused`, `credentials_withheld`, an
applied lifecycle hold, pause expiry, or `self_recovery` blockers.

## Creation, Lineage, and Resume Semantics

`start_session` returns `agent_uuid` (server identity anchor for this process),
`client_session_id` (proof string for this running process — never cross-process
selfhood), `continuity_token` (short-lived possession proof for PATH 0
anti-hijack rebinds: 1-hour rolling TTL, does not establish process-instance
continuity by itself), and diagnostics (`session_resolution_source`,
`identity_assurance`, `deprecations`). Use raw `onboard(...)` only for older
servers or unwrapped handler payloads.

### Lineage rules

- Declaring a currently-live agent as parent for a succession is rejected
  (`lineage_coincidental_rejected`): a live agent is a concurrent sibling, not a
  predecessor.
- Registered dispatched-child reasons (`subagent`, internal `dialectic_reviewer`,
  and `dispatch`) plus `compaction` continuation are exempt because their parent
  is legitimately live; unknown reasons receive no exemption.
- Use `spawn_reason="explicit"` for a deliberate handoff from an exited
  predecessor; `new_session` remains succession-shaped but does not by itself
  prove intentional lineage. A genuine handoff stays provisional until R1
  confirms it.
- Continuing the same still-running process means reusing the active binding or
  `client_session_id`, not minting another child.

### Refused and deprecated resume patterns

- **Bare `identity(agent_uuid=X, resume=true)`**: unsigned UUID claim. Refused
  under the default `UNITARES_IDENTITY_STRICT=strict`; a server set to `log`
  accepts it and emits hijack-suspected telemetry.
- **`onboard(continuity_token=...)` as cross-process resume**: S1-c refuses it
  (`status: continuity_token_resume_rejected`; the S1-a deprecation window has
  closed). Declare lineage with `parent_agent_id` for a new process, or rebind
  the same live process with `identity(agent_uuid, continuity_token, resume=true)`.
- **Bare `onboard()`**: older servers may pin-resume on weak session/IP:UA
  signals; current servers refuse weak-signal resumes (`resume_proof_required`).
  Use `start_session(force_new=true)` for a new process identity.
- **Stateless/OAuth transport nuances**: bearer-token `/mcp/` with no session
  header keeps no binding between calls, and an OAuth client's binding is per
  client (shared across its conversations), not per process. Pass
  `client_session_id` on ordinary calls; reserve `continuity_token` for explicit
  proof-owned rebinds.

## Transport Binding (`bind_session`) and Identity Verification

- Session binding uses a client-sent transport session signal (e.g., per-process
  `X-Session-ID`, never a static cross-process value; stateless `/mcp/` issues
  no protocol session of its own), `client_session_id`, or `continuity_token`.
- Binding a transport session is explicit (`bind_session`, not a side effect of
  `identity()`) and can be **refused**:
  - When the destination key resolves from a store keyed on User-Agent alone, it
    may belong to another caller, so `bind_session` returns `bound: false` with
    `rebind_refused` naming the source.
  - A destination that is another agent's stable `agent-...` session ID (e.g.
    via `X-Session-ID`) is refused as `rebind_refused: "foreign_stable_session_id"`,
    whichever transport header carried it. Your identity is unchanged; do not
    retry or forge — retry from a client sending its own session identifier.
- Never call `identity()` with no arguments: a call carrying no proof signal is
  gated to a fresh mint (`[FRESH_INSTANCE]`, S13) to keep unauthenticated reads
  off the User-Agent pin path, returning a newly created identity and leaving a
  spurious record. Always pass `identity(client_session_id="<your id>")`.
- Trust `identity()` only when `identity_assurance.caller_proven` is true. A
  `weak` tier with `proof_origin: "server_inferred"` means the server guessed;
  under strict identity it returns `client_session_id: null`, no
  `continuity_token`, and `credentials_withheld`.
- Inspect `identity_status`, `bound_identity`, `session_resolution_source`,
  `continuity_token_supported`, `identity_assurance`, and `deprecations`. If an
  already-onboarded process resolved only by weak fingerprinting, pass its
  `client_session_id` on the next call rather than minting a second identity.

## Applied Lifecycle Holds and Recovery Gates

- **While paused (before expiry)**: check-ins and new shared-memory entries are
  refused (not queued); dialectic moves still work. Because a paused agent
  cannot write the check-in that would lower its risk, its risk stays frozen at
  the reading that paused it: `self_recovery(action="quick")` and
  `self_recovery(action="review")` succeed only when that reading is already
  under their gates (`review` refuses at risk `0.65` and above, except for the
  legacy cold-start trap; `self_recovery(action="check")` reports blockers).
  Otherwise, the dialectic review opened for the pause or an operator ends it.
- **At pause expiry**: the next check-in awaits an exact-token durable resume
  and can then proceed. A synchronous shared-memory write starts that same
  resume, but the current write stays refused and must be resubmitted after the
  pause lifts.
- **Self-review boundary**: self-review may record useful reflection, but it
  cannot close a dialectic session bound to that identity's pause. An
  independent reviewer or operator must authorize the terminal decision; stale
  local status does not weaken that boundary.
- **`legacy_unbound_session`**: a completed review reporting
  `legacy_unbound_session` predates durable pause-generation binding and
  intentionally changed no current pause. Open a fresh review for the current
  pause instead of treating that result as a resume.
- **Operator override (`operator_resume_agent`)**: operator-only; refuses an
  active void or risk above `0.80` ("requires human intervention"), and requires
  `force=true` above `0.60`. Never resume your own pause via an operator path.
- **Authoritative checks vs. `C(V)`**: recovery checks risk, active void,
  status, ownership, and (for review recovery) reflection/persistence evidence.
  Legacy `C(V)` stays visible with source/role provenance but cannot authorize
  or deny recovery.
- **`recovery_needed` vs. `eligible`**: `self_recovery(action="check")`
  separates these. An active identity reports `recovery_needed=false` and
  `recovery_status=not_needed` even before its first check-in (no reflection
  needed). Inspect `risk_authority` to distinguish an unmeasured first state
  from lost risk evidence.
