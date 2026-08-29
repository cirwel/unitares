# BEAM verbs as contract capabilities — v0

**Status: draft, design-only. No code, no decision.** Nothing here authorizes a
cutover, changes Wave 3's scope, or proposes retiring an HTTP route. It asks one
question and proposes an answer to be argued with.

**Review: two adversarial passes (2026-08-28), no peer review.** The document
was written in a single pass from a reading of the tree. The first source
re-derivation folded four blockers where they land: the mint-time assurance
gate (§2, §7), `inbox` being a consuming mutation with an unresolved ack/retry
gap (§8, §9, §11), the contract-representability gap (§4), and a corrected risk
statement for widening `validate_path` (§9). A second source re-derivation found
four more constraints: an agent-facing lease mutation cannot inherit the live
route's permissive `:log` authorization (§7), `status` needs an agent-visible
scope and redaction contract (§3, §7), timer separation belongs at the
accountable dispatch boundary rather than in a duplicate SDK client (§5, §6),
and retries need a stable logical operation with a fresh single-use attestation
per attempt (§8). §5 remains an operator decision this document deliberately
does not take. Claims outside the reviewed citations should still be treated as
unverified until re-derived from the tree.

One `consult(purpose="critique")` pass was run on 2026-08-28 (consultation
`d1e98e34`, route `ollama` / `gemma4:latest`, `cost_class: local_free`,
degraded from `thorough` to `standard_local` because `privacy=local` forbids the
cloud lane). **That is advisory tool evidence and explicitly not peer review** —
the tool returns `can_satisfy_peer_review: false`, and `request_review` is the
governed verb. Three of its findings survived checking and are folded into §2,
§3 and §11; §9's `validate_path` prerequisite was found while checking it, not
by it. Two of its objections were refuted against the source and are recorded in
§13 so the next reader does not re-raise them.

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
The attestation stops being something an agent must orchestrate.

**The collapse is only sound if the mint keeps the assurance bar the four-step
version already sets — and "the middleware has resolved the caller" does not
clear it.** Step 2 is not ceremony: `/v1/lease-holder/attest` refuses to mint
unless `recertify_strong_tier` passes — a valid, *unexpired* continuity token
whose `aid` equals the claimed holder, i.e. a caller-presented cryptographic
proof (`src/http_routes/lease_identity.py:110-122`). A resolved MCP identity
can be a much weaker thing: the middleware will resolve a caller from a
server-inferred binding — a transport-fingerprint pin, a sticky-cache hit, a
transport-injected CSID — and the assurance layer marks exactly those
`caller_proven: false` and never-strong
(`src/mcp_handlers/updates/phases.py:136-152`). An internal mint that signs
whatever the middleware resolved would launder that weak binding into a strict
BEAM proof: `authorize_strict` on the msg routes would then grant sender
identity — and another agent's inbox — on the strength of a fingerprint guess.
**Rule: the internal mint gates on strong, `caller_proven` assurance, the same
bar `recertify_strong_tier` enforces on the HTTP path, and refuses anything
less.** What the capability collapses is the agent-side orchestration, never
the proof requirement.

The reason collapsing is safe rather than merely convenient is that the
attestation is **request-bound**: its claims cover the method, the path, and the
SHA-256 of the exact body bytes, and it "cannot be replayed even within the
validity window" (`src/lease_attestation.py:14-16`), with BEAM returning
`:identity_proof_replayed` on a reused proof. A collapsed call therefore carries
exactly the same binding as the four-step version — the steps were never what
made the proof strong.

There is a second-order reason, and it is the stronger one. Agent-to-agent
messaging ran over the knowledge graph for months, and
[`agent-message-transport-v0.md`](agent-message-transport-v0.md) documents the
damage: on 2026-08-27, 49 of 60 KG writes were channel traffic, evicting genuine
open findings from `scan-actors.sh`'s six-row window.

Why the KG? The honest version of this claim is weaker than it wants to be. What
is measured is that the KG *was* the reachable surface and *was* the one used;
the inference that reachability is what selected it is a claim about motive, and
no telemetry establishes motive. **Read it as the best available explanation,
not as a measured cause** — §12's first disconfirmer is precisely the test that
would refute it. It is offered because the alternative explanations are weaker,
not because it is proven: nobody chose the KG for its addressing, its read
state, or its lifetime, since it has none of the three.

If that reading holds, leaving the correct substrate behind a bearer token and a
signing dance recreates the same pressure, aimed the other way.

## 3. The proposed split

The discriminator is **not** "is it implemented on BEAM."

It is honestly **three** tests. An earlier draft hid the first by listing
"operator authority" as though it were a caller shape. It is not — an operator
is a caller like any other; what excludes force-release is the *authority the
action requires*, which is a property of the operation. The third test prevents
"an agent can choose it" from externalizing every low-level state transition.
So, in order:

1. **Does the action require an authority an agent cannot hold?** If yes, it is
   never a capability, whoever calls it. This is what excludes force-release,
   and it would still exclude it if only agents ever called it.
2. **Is there an agent mid-loop who decides to do this?** If no, there is no
   caller for a tool to serve, and it stays plane machinery.
3. **Can the operation be expressed as stable agent intent without making the
   caller mirror plane lifecycle or receive internal state?** If no, it is not
   ready for exposure even when test 2 passes. A consuming inbox needs
   acknowledgement semantics; a lease mutation needs strict object ownership;
   a status read needs an explicit visibility contract.

Substrate never enters any test. Keeping them separate matters because they
disagree on at least one case: a lease *handoff* is an agent decision (test 2
passes) whose completion is enforced by a timeout supervisor the agent does not
control, which is why §11 flags it as the least certain row below.

**Candidates for exposure** — agent decisions, subject to the blockers below:

- `msg/send` — after idempotency and principal derivation are defined
- `msg/inbox` — after claim/ack/redelivery and principal derivation are defined
- `lease/status` — separately, after its agent-visible response is scoped and
  redacted
- lease mutations — `acquire`, `release`, `handoff/offer`, `handoff/accept` —
  only after strict ownership and §6's activity-provenance boundary are real
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

The end state this document wants is a capability record in
`unitares.interface-contract.v1` whose coordination authority is the BEAM
route and whose agent-facing adapter preserves governance assurance. The
adapter is part of the security boundary, not a disposable forwarding shim.
**That composite implementation is not representable today, and an earlier
draft of this section overstated how close it is.** `_capability_record`
(`src/interface_contract.py:155`) can only describe tools that exist in the
`@mcp_tool` registry:
`get_public_tool_definitions` filters every definition against that registry,
the record's `implementation` field is the alias-resolved *Python tool name*,
its `kind` is only ever `workflow_alias` or `canonical_tool`, and
`transport_names` assigns the same public name to every transport. Nothing in
the contract can name a BEAM route as an implementation, and nothing in the
dispatcher can route a contract entry anywhere except a registered handler. So
the choice is explicit, not assumed away:

- **(a) Extend the contract and the dispatcher** — a new record `kind` (e.g.
  `beam_route`) or backend-authority field names the route while an adapter
  still performs assurance, principal derivation, exact-byte serialization,
  minting, and error normalization. Honest about the composite end state, but
  real surgery on `interface_contract.py` and the call dispatcher, both
  CI-pinned artifacts with a checked-in canonical serialization.
- **(b) Accept a thin registered security adapter** — a `@mcp_tool` handler
  performs those same checks and forwards the authorized operation. It is
  representable today with zero contract changes; the record truthfully reports
  `canonical_tool`, while BEAM remains the coordination authority.

This document leans **(b)** for the first slice — contract machinery should be
extended for a second consumer, not speculatively for the first, and naming a
route directly must never imply that governance's security adapter may be
bypassed. It records the disagreement with its own earlier draft rather than
hiding it.

Two conventions this must follow:

- **Consolidated action-router tools**, one per noun (`msg`, `lease`) — like
  `knowledge` and `dialectic` — not eight new names on a registry that already
  carries 66 registered tools, 30 of them advertised in the default `lite`
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

- **(a)** One explicit capability dispatch counts once; the outbox forwarder
  stops projecting lease-plane execution events into `audit.tool_usage`, so
  there is one row per logical agent act. Plane events remain canonical in
  `lease_plane.lease_plane_events` and carry correlation provenance instead.
- **(b)** Capability calls count, forwarder rows stay, and every reader splits on
  provenance — the #1955 fix generalized rather than repeated per reader.
- **(c)** Capability calls are recorded under a separate key entirely and never
  enter the tool aggregate.

### ✅ Operator decision, 2026-08-29 — (c)

**Capability calls are recorded under their own key and never enter the tool
aggregate.** Recorded as an operator choice, taken before any exposure work and
not derived from analysis in this document.

The stated reason: `audit.tool_usage` keeps meaning "an agent called a tool,"
which is the reading `adoption_kpi` and the doctor's failure classifier already
depend on, and no reader has to remember to split. (b) was declined precisely
because its failure mode is the one #1955 measured — readers *forgetting* the
split — and generalizing a fix does not stop the next reader from being written
without it. (a) was declined for this slice as a larger coordinated change to
the forwarder, not because it is wrong.

The accepted cost, stated so nobody rediscovers it as a defect: **capability
volume is invisible in the existing tool views until a view is built for it.**
A zero in those views will therefore mean "not counted here," never "not used" —
state 3 of the four the shared contract names, and not evidence of anything
about demand.

This closes §5 as a blocker on sequencing step 1. It does not close §8.

## 6. ⛔ The loop-detection hazard

Distinct from §5 and worse, because it reaches a governance decision rather than
a dashboard. `agent_presence_lease.py` keeps its heartbeat off the tool /
check-in / activity path so it "cannot feed loop-detection or the auto-heartbeat
activity tracker (the Dec `reply_to_question`/dialectic false-positive class)."

A lease *heartbeat* is substrate, emitted on a timer, and must never look like
agent action. A lease *acquire* is an agent decision and legitimately is one.
**Any exposure must keep the heartbeat off the tool path even when `acquire`
sits on it.** The separation belongs at the accountable entrypoint, not in a
duplicate transport client: `LeasePlaneClient.acquire()` and `.heartbeat()` are
already distinct methods that share only the low-level `_request_json` helper.
Sharing that helper is safe; sending scheduled maintenance through the
registered `lease` handler is not
(`agents/sdk/src/unitares_sdk/lease_plane/client.py`).

The implementation invariant, whichever §5 accounting candidate the operator
chooses, is:

- one explicit capability dispatch may write one tool-usage row;
- an automatic acquire, renew, heartbeat, timeout, reaper, or poll writes zero
  tool-usage rows and never feeds loop detection or activity tracking;
- BEAM execution events remain plane telemetry and carry a bounded origin
  discriminator plus a logical-operation correlation ID, rather than being
  reinterpreted as agent intent from their endpoint name; and
- a low-level client may be shared by both entrypoints, but the tool recorder
  and governance-activity consumers may only observe the explicit one.

## 7. Identity: what changes, what must not

**Invariant: governance mints, the agent never holds a credential.** This is the
half of `agent_presence_lease.py` that generalizes. An agent must not be handed
`LEASE_PLANE_BEARER_TOKEN`, a signing seed, or a `lat.v1.*` attestation.

**Invariant: minting requires strong, caller-proven assurance (§2).** The
middleware having resolved *an* identity is not sufficient input to the mint.
The internal mint gates on the bar `recertify_strong_tier` sets on the HTTP
path today — a caller-presented, unexpired cryptographic proof — and a
server-inferred binding (`caller_proven: false`: fingerprint pin, sticky-cache
hit, injected CSID) is refused, not signed. A test must pin that the capability
path cannot mint from a binding the attest route would have rejected.

**Invariant: agent-facing authorization is fail-closed even while an existing
raw route remains in compatibility mode.** The lease surfaces run
`IdentityBinding.authorize/4`, which is env-gated across `:off` / `:log` /
`:enforce`, and the live plane runs `:log` — verify, warn, serve anyway. That is
an existing server-caller rollout posture, not a safe authorization contract
for new agent reachability. A strong attestation authenticates the caller; it
does not authorize that caller to release or hand off another holder's lease
when BEAM merely logs the mismatch. Existing raw-route behavior need not change
as a side effect of exposure, but every agent-facing mutation must enforce
holder/recipient ownership in the same serialized operation as the BEAM
mutation. A Python preflight, or a BEAM check followed by an independently
scheduled mutation, is insufficient because either creates a
time-of-check/time-of-use gap. The mechanism — a strict agent route, a strict
dispatch context, or an equivalent BEAM-side gate that passes the authenticated
principal into the mutation — remains implementation work; fail-closed atomic
ownership does not.

**Invariant: acting-principal fields are derived, not caller-authored.** The
agent-facing schemas do not accept `sender_agent_uuid`, the inbox's
`recipient_agent_uuid`, or an acquire's `holder_agent_uuid`; the adapter derives
them from the same strong caller-proven identity that gates minting. Release and
handoff may accept opaque object IDs, but BEAM must strictly verify the current
holder or intended recipient before mutating. The existing msg routes already
use `authorize_strict/3`; a wrapper must preserve that strictness without asking
the agent to restate who it is.

**Blocker: define the agent-visible `lease/status` view.** The raw status route
is bearer-authenticated but not identity-bound, and `present_lease` includes
`holder_agent_uuid`, `holder_pid`, `audit_session`, and `substrate_state`.
Read-only is not the same as safe to expose. Before `status` becomes a
capability, the contract must state which surfaces a caller may query and which
fields are returned or redacted; wrapping the raw response is not acceptable.

Boundary tests must prove that weak assurance cannot mint, caller-authored
principal fields cannot change the acting identity, every mismatched
holder/recipient mutation refuses even while the compatibility route remains in
`:log`, and the status capability cannot return fields outside its declared
view. Those are conformance tests for the boundary, not a commitment to a route
or adapter design.

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

**Refuse-not-fallback does not resolve the ambiguous-commit case, and `msg` has
two of them.** `inbox` is not a read: `Repo.inbox/2` atomically claims a
recipient's pending rows — one statement sets `delivery_state='delivered'`
before returning them (`elixir/.../repo.ex:653-690`). A timeout between BEAM's
commit and the caller's receipt permanently consumes mail nobody saw, and
refusing the retry does not bring those rows back. `send` takes no
caller-supplied idempotency key — `send_message` inserts unconditionally with a
server-generated `message_id` (`repo.ex:595`) — so retrying an ambiguous
timeout can post the same message twice. Neither gap is an argument for keeping
the credential dance (the raw HTTP caller faces both today), but a capability
makes retries *routine* — adapters retry on timeout; a human copy-pasting curl
does not — so the first slice cannot ship on "refuse" alone. Candidates, named
here and deliberately not chosen: a two-phase claim/ack (inbox returns rows
under a delivery lease, a second call acknowledges, unacked rows redeliver
after a timeout); and an idempotency key on `send` (caller-supplied, uniqueness
enforced by the transport). Defining these semantics is part of §9 step 1's
design work; "refuse" only covers the case where the mutation is known not to
have happened.

**Retry invariant: the logical operation is stable and the authorization is
fresh.** Every retry carries the same server-enforced operation identity — a
send idempotency key, a claim/ack token, or an existing lease operation's
stable identity — but a newly minted single-use `lat.v1.*` attestation bound to
that attempt's exact bytes. An operation with no stable retry identity remains
blocked until one is defined. Reusing the attestation turns lost-response
recovery into a replay refusal; changing the operation identity turns it into a
duplicate mutation. Tests must pin both halves together.

## 9. Sequencing

1. **`msg/send` first.** It is the smallest surface with measured reachability
   pressure and already uses `authorize_strict`. It still waits for §5's
   accounting choice, a server-enforced idempotency key, derived sender
   identity, and §8's fresh-proof/stable-operation retry rule.
2. **`msg/inbox` second.** It additionally waits for claim/ack/redelivery
   semantics and a recipient derived from the authenticated caller.
3. **`lease/status` separately**, only after §7 defines its visibility and
   redacted response. Read-only does not make the raw route agent-safe.
4. **Lease mutations later, or never.** `acquire`, `release`, and handoff wait
   for §6's activity-provenance boundary, §7's strict BEAM-side ownership, and
   a decision that exposing the state machine is worth its drift cost (§11).
5. **Dialectic**: formalize the existing flag-gated client as a contract record.
   No behavior change.
6. **Effects and force-release**: not in scope, now or later.

The listed preconditions block each slice independently; landing one does not
silently authorize the next.

**Status after the 2026-08-29 operator decisions.** §5 is settled (see its
decision block), so step 1's remaining blockers are §8's ack/retry rule, a
server-enforced idempotency key, and derived sender identity — **§8 is now the
single largest open item in front of step 1.** Step 2 additionally waits on
claim/ack/redelivery. Steps 3 and 4 are deferred by the same decision, not
cancelled.

**Steps 1 and 2 also have a concrete prerequisite this document originally
missed.**
`mint_lease_attestation` refuses any path that is not `/v1/lease/...`
(`validate_path`, `src/lease_attestation.py:176-184`), so the server-side
minting helper that §7's invariant depends on **cannot mint a proof for
`/v1/msg/send` or `/v1/msg/inbox` today**. Be precise about what widening that
allowlist changes, because an earlier draft overstated it: every attestation
carries an exact `pth` claim and BEAM compares it against the exact request
path (`federated_identity_verifier.ex:95`, `claims["pth"] == context.path`),
so **no already-minted token gains an audience from a wider allowlist** — a
proof minted for one route can never be presented at another, whatever the
mint would now permit. What widening changes is the **signer's issuance
scope**: the set of routes governance is willing to bind future proofs to.
That is still a real security decision — per-route widening keeps the scope
enumerable, a prefix like `/v1/msg/` does not — and whoever takes either step
should treat it as their first design question, for that reason rather than
the wrong one.

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

- ~~**What "tool usage" means after exposure** (§5).~~ **Settled 2026-08-29** by
  operator decision (c): capability calls get their own key and never enter the
  tool aggregate. See §5. No longer blocking.
- **How §6's activity provenance is represented.** The invariant is settled:
  explicit dispatch is agent activity and timer maintenance is not. The bounded
  origin/correlation shape and where it is persisted remain design work.
- **How strict lease capability authorization reaches BEAM** (§7). A new route,
  strict dispatch context, or equivalent atomic gate are implementation choices;
  inheriting `:log` or relying on a Python preflight are not.
- **What `lease/status` may reveal** (§7). Query scope and the redacted response
  are unresolved; the raw response is not the capability contract.
- **Latency.** Agent → MCP → Python → BEAM re-introduces exactly the boundary
  the BEAM move was meant to escape, and `beam-footprint-roadmap-v0.md`'s stop
  sign #4 ("if the Ports/HTTP boundary accrues >1 distinct workaround pattern,
  the boundary design is wrong — halt") applies recursively here. Unmeasured in
  this document. Note that latency is retired as a *decision* gate for Wave 3
  and this does not revive it; it is named as a correctness concern under stop
  sign #4, which the v0.4 resolution explicitly kept live.
- **Whether the contract becomes a replica of the state machine.** The strongest
  general case against exposing any mutating verb: `acquire`, `release`,
  `handoff` and `resolve` are all state transitions whose correctness depends on
  internal temporal logic — timeouts, supervisors, reaper cadence. Publishing
  them as capabilities makes the agent-facing contract an externalized copy of
  the plane's state machine, and a copy is free to drift from its original.
  `status` is genuinely read-only and does not carry this risk. `inbox` was
  mis-filed alongside it in an earlier draft: it is a consuming mutation with a
  delivery-state transition behind it (§8) and belongs with the mutating verbs.
  `msg send` has no state machine behind it, but §8's idempotency gap is its
  own version of the same cost.
- **Whether `lease` is worth exposing at all.** §§6–7 and the state-machine
  drift cost may make it expensive enough that the honest answer is "`msg` yes,
  `lease` no." That would be a fine outcome for this document.
  **Operator decision, 2026-08-29: `msg` proceeds now; `lease` stays a later
  slice, deliberately not cancelled.**

  **The operator's stated reason is a footprint argument, not a cost one:**
  exposing the lease verbs as capabilities would propagate BEAM further, and
  that expansion is the thing being held back. This is a different reason from
  the ones this document had assembled — §§6–7's design cost and the
  state-machine drift risk — and it is the reason of record. Those remain true
  and remain live design work; they are not why the slice was deferred.

  The distinction matters for anyone who later argues the deferral away. Solving
  §6 and §7 would remove the *cost* objection while leaving the *footprint*
  objection untouched, so a future "the blockers are cleared, ship it" does not
  follow from clearing them. The question above therefore stays genuinely open,
  and deferring the slice is not a quiet yes to it.

## 12. How to know this was wrong

- If, after `msg` ships as a capability, agents keep reaching for the KG or a
  raw HTTP call for coordination traffic, then reachability was not the binding
  constraint and §2's argument is refuted.
- If the §5 accounting choice has to be revisited within one month of shipping,
  the choice was made too fast and should have been an operator decision taken
  earlier, not later.
- If a timer-driven acquire, renew, heartbeat, timeout, reaper, or poll produces
  a tool-usage row or feeds activity/loop detection, §6's boundary failed.
- If an agent-facing mutation can commit without strict current-holder or
  intended-recipient authorization at BEAM, §7's boundary failed — even when
  the caller itself carried a valid strong attestation.
- If `lease/status` exposes the raw lease record without an explicit query scope
  and field-level contract, the read boundary failed.

## 13. Objections already checked and refuted

Recorded so the next reader does not spend the same effort. Both came from the
§0 advisory pass; both are wrong against the source, and saying so is cheaper
than letting them resurface.

**"Collapsing the credential dance risks a stale token used outside its intended
window."** No. The attestation is request-bound to method, path, and body hash,
and is non-replayable even inside its validity window
(`src/lease_attestation.py:14-16`); BEAM answers a reused proof with
`:identity_proof_replayed` (`identity_binding.ex`). The four steps were never
what bound the proof to the request — the claims are.

**"The KG failure was a data-modelling problem, so moving substrate does not
address it; the proposal assumes the new plane has unbounded capacity."** The
first half misreads the fix and the second describes a claim this document does
not make. The damage was specifically that message bodies sat in an
FTS-indexed `summary` column, and the replacement transport puts the body in an
`envelope` that is **not searchable by default**, caps it at 64 KB, and bounds
its lifetime at 7 days (`agent-message-transport-v0.md` §1, §4). Substrate and
data model moved together, which is the whole content of that change.

**What the same pass got right** is folded above rather than answered here: the
caller-shape discriminator was concealing a second test (§3), and the
state-machine-replica argument against exposing mutating verbs (§11) is the
strongest general objection on the table.
