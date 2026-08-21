# Cedar Delegation Authorization — v0

**Created:** 2026-08-21 · **Status:** Draft v0 — design only, decision pending.
Nothing in this document is an implementation order; the open decisions in §8
belong to the operator.

## 1. Problem

UNITARES deliberately has no authorization layer. Strict identity is an
*accountability* gate — writes must be attributable to a proven process — but
nothing decides whether a given caller *may* perform a given action on a given
surface. The scope doc is candid about this (single-operator, internal fleet
hygiene, no tenant boundary), and the roadmap carries multi-principal trust as
a separate future track.

Three pressures now point at the same missing piece:

1. **The delegation question.** The mature answer to "may this agent touch
   this?" — argued independently by AWS's Context Ontology Accelerator and
   consistent with this repo's own proof-origin discipline — is to evaluate
   the **delegating principal**, not the agent: agent identity is logged and
   accounted, never trusted as a grant.
2. **Hand-rolled policy fragments already exist.** `src/mcp_handlers/stakes_table.py`
   is a `(tool, action) → high|baseline` classification that fails closed on
   unknown actions; `docs/proposals/track-b-operator-delegate-design.md` is a
   hand-written scope policy; the #425 identity-requirement map and the #775
   stakes gate both key on the same `(canonical_tool, action)` seam. These are
   policy-engine inputs with no engine.
3. **The governed-effect plane needs a veto it does not have.** The plane
   contract's governance-veto check (`governed-effect-plane-v0.md` §6) is a
   named build prerequisite for `execute` custody and exists nowhere today.

## 2. What Cedar is, and why it passes the execution-cost policy

[Cedar](https://github.com/cedar-policy/cedar) is an open-source (Apache-2.0)
policy language and evaluation engine, written in Rust, evaluating entirely
locally — no service, no key, no metered API, so Rule 6 is satisfied. Python
bindings exist ([cedarpy](https://github.com/k9securityio/cedar-py), PyPI
wheels for Linux/mac/Windows). The one supply-chain caveat: the bindings are
third-party-maintained. The dependency ships as an **optional extra**
(`pip install unitares[cedar]`); the core install never imports it.

## 3. Position — one policy engine, not a third primitive

This repo already carries two adjacent governance primitives: the strict
identity write gate (accountability) and the fermata / governed-effect track
(custody of side effects; fermata owns the canonical IR per the 2026-06-28
convergence decision). Cedar must not become a third parallel brain.

**Proposed position: Cedar is the shared policy engine of the family, with one
policy set evaluated at two enforcement points:**

| Enforcement point | Question | Granularity | Exists today |
|---|---|---|---|
| Pre-dispatch `authorize` middleware step | may this call proceed? | every tool call, sync | no (this proposal) |
| Governed-effect veto check (plane §6) | may this effect commit? | per effect, async | no (named prerequisite) |

Boundary discipline, per the convergence doc's own rule (cross-reference, do
not couple; coupling is an explicit decision):

- Fermata's core IR stays identity-agnostic. Cedar's action/resource model
  maps onto the core's capability/scope vocabulary; UNITARES-specific context
  (assurance tier, proof origin) rides only the `unitares` profile.
- `src/substrate/vouch.py` (orchestrator-vouched bindings) and the operator
  token are usable delegation carriers here directly.
- `src/effect_grant.py` belongs to the governed-effect machinery. This
  proposal **references** it as a future carrier and does not consume it;
  wiring it in is a recorded coupling decision, not a side effect.

## 4. Request model

Cedar evaluates `(principal, action, resource, context)`:

- **Principal — the delegation chain's root, never the agent's say-so.**
  Explicit carriers only: `Operator::"<token-id>"` (env-allowlisted bearer,
  #425), a vouched binding (`core.vouched_bindings`: child vouched by parent,
  expiring), or — later, by explicit decision — an effect grant. A caller with
  no carrier evaluates as `Agent::"<uuid>"` with no delegated powers.
  ⛔ **`parent_agent_id` / lineage is never a delegation carrier.** Lineage
  means "inherited work from", not "acting on behalf of" (glossary); inferring
  authority from it recreates the #679 laundering class in authorization form.
- **Action — the #1387 canonical pair.** `resolve_canonical_action_and_source`
  yields a server-clamped `(canonical_tool, action)` (unknown actions clamp to
  `action_unlisted`), giving a bounded namespace: `Action::"dialectic::request"`,
  `Action::"knowledge::store"`. The same seam already feeds the #425 identity
  gate, the #775 stakes gate, and the audit payload — one vocabulary, four
  consumers, zero drift surface.
- **Resource — start coarse.** v0 resources are surface classes
  (`Surface::"knowledge_graph"`, `Surface::"config"`, `Surface::"lifecycle"`),
  derived from the tool. Row-level resources (a specific discovery, a specific
  agent) are out of scope until a policy needs one.
- **Context — assurance as attributes, not authority.** `identity_assurance.tier`,
  `proof_origin`, `caller_proven` enter as context attributes so policies can
  require e.g. `context.caller_proven == true` for high-stakes actions. The
  strict-identity write gate itself is unchanged and remains authoritative
  independently of Cedar (#679; defense in depth, not replacement).

## 5. Enforcement point

A new `authorize` step appended to `PRE_DISPATCH_STEPS`
(`src/mcp_handlers/middleware/__init__.py`) after `validate_params`: identity
is resolved, the alias is canonicalized, params are validated, and a step
returning a response short-circuits dispatch — which is exactly the DENY
shape. One step covers the MCP wrapper and the REST dispatch fallback; the
transport-contract census (#1764) keeps new surfaces honest.

A Cedar DENY returns the standard typed refusal envelope: the failing policy
id, the principal actually evaluated, and a recovery block — never a bare
rejection.

## 6. Rollout — shadow first, writes only, fail open

- **Phase 0 — shadow.** `GOVERNANCE_CEDAR_SHADOW` (the existing `*_SHADOW`
  precedent in `config/governance_config.py`): evaluate on every dispatch, log
  the would-be decision (policy id, principal, action) into the telemetry
  envelope and the tool-usage payload; enforce nothing. Denial counts are
  telemetry that informs — per the measurement-authority rules they carry no
  removal or enablement authority by themselves.
- **Phase 1 — enforce on governed write surfaces only**, mirroring "reads open,
  writes accountable". Reads and `pre_onboard` discovery tools stay ungated.
- **Phase 2 — the effect-plane veto**: the plane's §6 check calls the same
  policy set with `custody_mode` in context. Sequenced with the plane's own
  build gates, not ahead of them.

**Defaults preserve the residentless install:** no policy directory → allow
everything, exactly today's behavior; the suite's companion assertion lives in
`tests/test_residentless_install.py`. Engine errors **fail open** with a
telemetry mark, for the same reason the assessment pipeline does
(`src/mcp_handlers/core.py`): an infra blip must not mass-refuse the fleet.
Fail-open vs fail-closed for *high-stakes* actions specifically is an operator
decision (§8).

## 7. Seed policy

Translate `stakes_table.py` mechanically as the first policy file: high-stakes
`(tool, action)` pairs require `context.caller_proven == true`; baseline pairs
permit. This changes nothing in shadow mode and gives Phase 0 real decisions
to log. `track-b-operator-delegate-design.md`'s read-only delegate scope is
the natural second policy and the first real use of a delegation carrier.

## 8. Open decisions (operator's)

1. **Adopt at all?** This entire proposal is deferred-by-default; per the
   anti-inert-capability rule it should not be built until an authorization
   question is actually pressing (a second principal, a delegate, or the
   effect-plane veto build).
2. **Policy ownership:** portable (fermata-profile) vs UNITARES-local policy
   files. §3 recommends portable core + profile context, but that couples to
   fermata's contract cadence.
3. **`effect_grant.py` coupling** — consume as a carrier, or leave to the
   effect plane.
4. **High-stakes failure mode** — fail open with telemetry (consistent) vs
   fail closed (safer, mass-refusal risk).
5. **Dependency acceptance** — third-party cedarpy maintenance risk vs
   vendoring vs waiting for first-party bindings.

## 9. What this is not

Not a tenant boundary, not cross-operator trust (roadmap's multi-principal
track has its own evidence path), not a replacement for the strict-identity
write gate, not a change to the governed-effect contract, and not a metered
dependency. It is one engine for policy questions the codebase is already
asking in three hand-rolled places.
