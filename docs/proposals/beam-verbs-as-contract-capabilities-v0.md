# BEAM verbs as contract capabilities — v0

**Status: draft, design-only. No code, no decision.** Nothing here authorizes a
cutover, changes Wave 3's scope, or proposes retiring an HTTP route. It asks one
question and proposes an answer to be argued with.

**Review: none.** Written in a single pass from a reading of the tree. The
adversarial reviews this repo normally expects on lease-plane / BEAM scope have
not been run against it, and §5 and §6 are the two sections where that absence
matters most — both are operator decisions this document deliberately does not
take. Treat every claim here as unverified until someone re-derives it from the
cited call sites.

**The question.** Which lease-plane (BEAM) operations should become agent-facing
capabilities in `unitares.interface-contract.v1`, which must stay reachable only
as plane machinery, and what has to be settled *before* either.

---

## 1. Why this exists

The tree currently holds **three different answers to this question**, none of
them written down as a rule:

| Surface | Reachable how | Verdict encoded in the code |
| --- | --- | --- |
| Dialectic phase / resolve | `dialectic` MCP tool → Python → `POST /v1/dialectic/{phase,resolve}` (`src/mcp_handlers/dialectic/beam_resolve_client.py`, called from `dialectic/session.py:204` and `handlers.py:134`) | BEAM verb behind a capability — **yes** |
| `agent:/` presence lease | Server-side side-effect on the onboard/check-in path (`src/mcp_handlers/identity/agent_presence_lease.py`) | BEAM verb behind a capability — **deliberately no** |
| `/v1/msg/send`, `/v1/msg/inbox` | Raw HTTP with out-of-band credentials only (`elixir/.../http_router.ex:644,694`) | **unanswered** |

The middle row is the one to read carefully, because it is easy to mistake for a
precedent and it is not one. `agent_presence_lease.py` *does* mint the identity
attestation server-side (`_mint_presence_attestation`, line 145) — that half is
the pattern this document wants to generalize. But its module docstring is
explicit that the call is kept **off** the tool path on purpose:

> The heartbeat is a raw HTTP side-effect to the lease plane. It deliberately
> does NOT route through any governance-tool / check-in / activity path, so it
> cannot feed loop-detection or the auto-heartbeat activity tracker (the Dec
> `reply_to_question`/dialectic false-positive class). It is a lease side-effect,
> not a high-impact agent action.

So the existing evidence says: *mint server-side, yes; put lease traffic on the
tool path, no.* Both halves are load-bearing and §5 and §6 are why.

## 2. The argument for exposing anything at all — the credential dance

For an agent to send a message on `/v1/msg/send` today, unassisted:

1. Establish identity — over MCP.
2. `POST /v1/lease-holder/attest` on the **Python** server, authenticated with
   `UNITARES_HTTP_API_TOKEN`, presenting its continuity proof. Governance
   verifies (`recertify_strong_tier`) and mints a request-bound Ed25519
   `lat.v1.*` attestation. Per `src/lease_attestation.py`, "the signing seed
   stays in governance."
3. `POST /v1/msg/send` on the **BEAM** node with the shared
   `LEASE_PLANE_BEARER_TOKEN` *and* `x-unitares-identity-proof`.
4. BEAM verifies via `FederatedIdentityVerifier` against governance-published
   keys.

Two servers, three credentials, four steps — and the identity that roots the
whole chain was established over MCP at step 0. **The chain exists because the
agent left the MCP session.** A capability collapses it: the identity middleware
has already resolved the caller, governance mints internally, one hop to BEAM.
Nothing about the verification weakens; the attestation stops being something an
agent must orchestrate.

There is a second-order reason, and it is the stronger one. Agent-to-agent
messaging ran over the knowledge graph for months, and
[`agent-message-transport-v0.md`](agent-message-transport-v0.md) documents the
damage: on 2026-08-27, 49 of 60 KG writes were channel traffic, evicting genuine
open findings from `scan-actors.sh`'s six-row window. Why the KG? Because the KG
was the surface an agent could already reach. **Reachability has already driven
substrate choice once, wrongly.** Leaving the correct substrate behind a bearer
token and a signing dance recreates the same pressure, aimed the other way.

## 3. The proposed split

The discriminator is **not** "is it implemented on BEAM." It is: *is the caller
an agent mid-loop making a governed decision, or is it plane machinery, or is it
operator authority?*

**Expose as capabilities** — agent decisions:

- `msg` — `send`, `inbox`
- `lease` — `acquire`, `release`, `status`, `handoff/offer`, `handoff/accept`
- dialectic session/phase/resolve — already exposed; formalize as a contract
  record rather than a flag-gated client

**Never expose** — operator authority:

- `/v1/lease/force-release`. `http_auth.ex` gives it a separate elevated token,
  mutually exclusive with the ordinary bearer, enforced at the auth layer
  ("standard bearer must NOT permit force-release"). Putting it in a model's
  context contradicts `agent-message-transport-v0.md` §3, "a message carries no
  authority."

**Never expose** — plane machinery with no agent caller:

- `effect_executor`, `effect_reconcile`, `effect_recovery`, `file_write_executor`
- `handoff_timeout`, `dialectic_saga_reaper`, `identity_nonce_reaper`,
  `dialectic_liveness`

These run *between* calls. They have no caller to be a tool for.

**Keep as an internal side-effect** — the `agent:/` presence heartbeat, for the
reason its docstring already gives (§6).

## 4. Shape

A capability record in `unitares.interface-contract.v1` whose canonical
implementation is the BEAM route — *not* a Python handler that exists only to
forward. `src/interface_contract.py:155` (`_capability_record`) already models a
capability as a public name + canonical implementation + per-transport spelling,
and the contract is transport-neutral by construction (MCP, `GET /v1/tools`,
stdio). The record is the right unit; a shim handler is not.

Two conventions this must follow:

- **Consolidated action-router tools**, one per noun (`msg`, `lease`) — like
  `knowledge` and `dialectic` — not eight new names on a registry that already
  carries 67 registered tools, 30 of them advertised in the default `lite`
  profile.
- **Stakes classification is mandatory, not optional.** Every new `(tool,
  action)` pair needs an entry in `src/mcp_handlers/stakes_table.py`, which
  fails closed to `high` for unknown pairs. The table is currently inert (no
  gate consults it) but is the port-survivable artifact, and `export_table()` is
  the single authoritative serialization point for the Elixir side when the gate
  mechanism is re-expressed there.

## 5. ⛔ The measurement hazard — settle this first

**BEAM operations are already forwarded into `audit.tool_usage`, and the last
time that happened it corrupted three independent readers.**

From #1955 (2026-08-28): `agent:/` presence leases — ~913/day, 100%
`holder_class=process_instance` — are forwarded into `audit.tool_usage` as
`lease.*` rows by the BEAM outbox forwarder. Every surface that aggregated that
table without splitting on `holder_class` read heartbeat substrate as
tool/coordination throughput:

- **4 of the top-10 "most used tools" were `lease.*` rows**, with
  `lease.acquire` at #2 (~45k/7d) — ~72% of non-poller "tool usage." The
  orchestrator's true coordination share is ~0.035% of lease rows all-time.
- **`adoption_kpi` onboard_conversion** counted `lease.*` as `did_anything`, so
  a heartbeat-only agent scored as active. Six agents flip over a live 14-day
  window; true bounce was understated.
- **`unitares_doctor` `failure_label_live`**: the forwarder hardcodes
  `success=true` on these rows, so in a partial outage heartbeat volume alone
  cleared the 10k floor and fired a WARN on rows that *cannot* be false.

This is not a metrics nit. `CLAUDE.md`'s measurement-authority section names the
adoption gate specifically as a surface whose zeros were produced three times in
one month by defects (#1414, #1424, #1442) and never once by real disinterest.
Making BEAM verbs into capabilities will put their volume under the same keys as
agent tool calls, on that exact surface.

**Rule: exposure must not be the change that decides what "tool usage" means.**
What counts as a tool call once a lease acquisition is a capability is a
deciding standard, and per the shared contract a deciding standard is stated as
a choice *before* it is applied, by the operator — not chosen silently and
reported afterwards as the method. This document therefore does **not** pick it.
It names it as the blocking question and proposes three candidates:

- **(a)** Capability calls count; the outbox forwarder stops writing `lease.*`
  rows for the same operations, so there is one row per logical act.
- **(b)** Capability calls count, forwarder rows stay, and every reader splits on
  provenance — the #1955 fix generalized rather than repeated per reader.
- **(c)** Capability calls are recorded under a separate key entirely and never
  enter the tool aggregate.

## 6. ⛔ The loop-detection hazard

Distinct from §5 and worse, because it reaches a governance decision rather than
a dashboard. `agent_presence_lease.py` keeps its heartbeat off the tool /
check-in / activity path so it "cannot feed loop-detection or the auto-heartbeat
activity tracker (the Dec `reply_to_question`/dialectic false-positive class)."

A lease *heartbeat* is substrate, emitted on a timer, and must never look like
agent action. A lease *acquire* is an agent decision and legitimately is one.
**Any exposure must keep the heartbeat off the tool path even when `acquire`
sits on it** — which means `lease` as a capability cannot simply wrap the SDK
client, because the client's heartbeat and acquire share a code path today.

## 7. Identity: what changes, what must not

**Invariant: governance mints, the agent never holds a credential.** This is the
half of `agent_presence_lease.py` that generalizes. An agent must not be handed
`LEASE_PLANE_BEARER_TOKEN`, a signing seed, or a `lat.v1.*` attestation.

**Non-goal: do not let exposure silently move a surface's identity mode.** The
lease surfaces run `IdentityBinding.authorize/4`, which is env-gated across
`:off` / `:log` / `:enforce`, and the live plane runs `:log` — verify, warn,
serve anyway. The msg routes run `authorize_strict/3` unconditionally, by
deliberate design, because the graduated modes on a mailbox mean handing one
agent's mail to another while logging a warning about it. A capability wrapper
must preserve each surface's declared mode exactly, and a test must pin that it
neither promotes nor demotes one.

## 8. Failure posture: no fallback for mutations

Wave 3a's proxy (`src/wave3a_beam_proxy.py`) uses a 500ms timeout then falls
back to the Python implementation. That is correct for the read-only handlers it
was designed for and **wrong for a lock**: a fallback that reimplements
exclusion is two lock implementations racing.

`beam_resolve_client.py` also falls back — to `pg_resolve_session` — and that
one is legitimate, but only because Python owns a B-4 guard protecting the
terminal row in either path. **The guard is what makes the fallback safe, and it
does not generalize.** For `lease` and `msg` mutations the failure posture is
refuse, not fall back.

## 9. Sequencing

1. **`msg` first.** Newest surface, no lease state machine, already
   `authorize_strict` unconditionally, and the credential dance is at its worst
   there. Smallest honest slice.
2. **`lease` second**, and only after §6 is resolved — the heartbeat/acquire
   split is real work, not a wrapper.
3. **Dialectic**: formalize the existing flag-gated client as a contract record.
   No behavior change.
4. **Effects and force-release**: not in scope, now or later.

§5 blocks step 1. §6 blocks step 2.

## 10. What this does not do

- Does **not** propose retiring any HTTP route. The orchestrator, watchdogs, and
  ops scripts are server-to-server callers and should not be pushed through an
  agent-facing protocol; the routes stay.
- Does **not** change Wave 3's committed scope or reopen any gate the
  2026-06-25 resolution closed.
- Does **not** propose removing any capability, and cites no usage count as
  evidence for removal. The #1955 figures in §5 are cited as *corruption of a
  measurement*, never as demand for a feature.

## 11. Open questions

- **What "tool usage" means after exposure** (§5). Operator decision. Blocking.
- **The heartbeat/acquire split** (§6). Design work, not yet done.
- **Latency.** Agent → MCP → Python → BEAM re-introduces exactly the boundary
  the BEAM move was meant to escape, and `beam-footprint-roadmap-v0.md`'s stop
  sign #4 ("if the Ports/HTTP boundary accrues >1 distinct workaround pattern,
  the boundary design is wrong — halt") applies recursively here. Unmeasured in
  this document. Note that latency is retired as a *decision* gate for Wave 3
  and this does not revive it; it is named as a correctness concern under stop
  sign #4, which the v0.4 resolution explicitly kept live.
- **Whether `lease` is worth exposing at all.** §6 may make it expensive enough
  that the honest answer is "`msg` yes, `lease` no." That would be a fine
  outcome for this document.

## 12. How to know this was wrong

- If, after `msg` ships as a capability, agents keep reaching for the KG or a
  raw HTTP call for coordination traffic, then reachability was not the binding
  constraint and §2's argument is refuted.
- If the §5 accounting choice has to be revisited within one month of shipping,
  the choice was made too fast and should have been an operator decision taken
  earlier, not later.
- If a capability wrapper is found to have changed any surface's identity mode
  (§7), this design is unsafe as written and the wrapper approach should be
  abandoned in favor of leaving the routes raw.
