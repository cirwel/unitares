# Plexus Scope

- **Created:** May 2, 2026
- **Last Updated:** May 23, 2026
- **Status:** Active boundary name over live Surface Lease Plane; Plexus Zero retained as manual fallback

---

## Definition

Plexus is the narrow TTL coordination layer for temporary ownership of shared mutable surfaces.

> **Defining sentence:** Plexus serves leases and revocation events. Everything else is a consumer.

All schema, API, `surface_id`, rollout, and implementation semantics remain defined by the Surface Lease Plane RFC. Plexus is the product/boundary name unless an extraction trigger from the BEAM Coordination Kernel plan fires.

Plexus exists to answer one operational question before an agent mutates a shared surface:

> May I touch this surface right now, who else claims it, when does that claim expire, and how do we release, revoke, or hand it off?

Plexus does **not** decide what the work means, what should be prioritized, whether a design is correct, or what belongs in durable memory. Those questions remain with UNITARES governance, Dialectic, KG, council review, PR review, and the operator.

## Current state

As of May 23, 2026, Plexus is not waiting on a separate bootstrap service. The active service is Surface Lease Plane in `elixir/lease_plane/`, loaded locally as `com.unitares.lease-plane`, with authenticated `/v1/health` returning `ok: true` in the operator check. Resident Phase B enforcement also ships through the lease-plane track, so the Plexus name should continue to describe that boundary rather than fork a new implementation.

## Why this document exists

The name `Plexus` emerged in Dispatch scratch planning while the canonical repo work already used `Surface Lease Plane` and `BEAM Coordination Kernel`. This document prevents a parallel implementation track by binding the names together:

| Name | Role | Canonical location |
|---|---|---|
| Plexus | Product/system boundary name for the narrow TTL coordination layer | This document |
| Surface Lease Plane | Current UNITARES contract/RFC and implementation track for leases | [`surface-lease-plane-v0.md`](surface-lease-plane-v0.md) |
| BEAM Coordination Kernel | Ontology/integration framing for the BEAM sidecar | [`../ontology/beam-coordination-kernel.md`](../ontology/beam-coordination-kernel.md) |
| UNITARES | Governance truth, identity, calibration, audit, dialectic, KG | Existing UNITARES governance stack |
| KG | Durable semantic memory: sediment, not chatter | KG tools/schema; future KG scope artifact |

Rule: **do not start a separate Plexus repo or service while the Surface Lease Plane remains the active implementation track.** If `Plexus` survives as a name, it names the narrow coordination boundary around that work.

## V1 owns

Plexus v1 owns only lease-lifecycle coordination for shared mutable surfaces:

- surface identifier canonicalization using Surface Lease Plane rules, without inventing a second namespace;
- lease acquire/status/query;
- lease renew/heartbeat;
- lease release;
- expiry/reaper behavior;
- revocation/force-release events as an umbrella term for the explicit release vocabulary specified by the Surface Lease Plane RFC;
- handoff as a lease-lifecycle primitive, where implemented by the active phase;
- the RFC-defined conflict response shape such as `held_by_other` with blocking lease, holder, expiry, and retry metadata;
- holder metadata imported from UNITARES identity, without minting identity;
- lease-lifecycle audit/telemetry events only.

Allowed v1 verbs are intentionally small: acquire, status/query, renew/heartbeat, release, revoke/force-release through the explicit operator path, and handoff where the active implementation phase supports it.

The initial forcing function is whole-file repo path coordination. Other surface kinds may remain in the broader Surface Lease Plane RFC, but Plexus should earn each additional kind through observed need and review rather than scope drift.

## V1 does not own

Plexus v1 does not own:

- identity issuance, lineage, continuity tokens, or display labels;
- EISV, calibration, governance verdicts, or trust-tier math;
- KG promotion, KG lifecycle, or durable semantic interpretation;
- Dialectic orchestration or design-review topology;
- Slack/Discord replacement, broad agent chat, inboxes, or presence feeds;
- project management, priority assignment, or task routing;
- broad multi-node distribution or Erlang clustering;
- dashboards as the first deliverable;
- arbitrary metadata stores attached to leases;
- force-release policy beyond the explicit operator/governance path already specified by the Surface Lease Plane RFC.

If a proposed feature is not necessary to prevent or resolve a shared-surface collision, it probably does not belong in Plexus v1.

## Plexus Zero: manual fallback protocol

When the service is unavailable, a caller is not integrated yet, or the operator explicitly needs a human-visible claim during recovery, use a manual paper protocol in the operator-visible coordination locus. For the current workstream, that means the active Discord thread or an explicitly named operator-visible board, not this file.

A manual claim uses this shape:

```text
CLAIM surface=<surface-id>
intent=<one sentence>
ttl=<duration, usually 30m>
evidence_ref=<thread/branch/session/PR/pre-existing KG note>
```

KG notes may be referenced only when they are already durable context. Do not create a KG note for each lease claim.

A completion uses this shape:

```text
RELEASE surface=<surface-id>
summary=<lease-lifecycle outcome, not a general work log>
```

A transfer uses this shape:

```text
HANDOFF surface=<surface-id>
to=<agent/person/session>
reason=<why>
freshness_horizon=<timestamp or duration>
```

A blocked attempt should not wait silently:

```text
BLOCKED surface=<surface-id>
held_by=<holder if known>
expires_at=<expiry if known>
next_action=<retry | ask operator | choose another surface>
```

Plexus Zero remains intentionally awkward. The friction teaches the real service what must become automatic.

## Fallback surface IDs

Use a tiny seed set when the manual fallback is active:

Manual Plexus Zero may use human-readable `repo://unitares/...` aliases in Discord. Those aliases are **not** service/API `surface_id` values. Any implementation or automated client must translate to the canonical Surface Lease Plane `file://` surface ID for an exact path.

| Manual Plexus Zero alias | Canonical service surface | Purpose | Notes |
|---|---|---|---|
| `repo://unitares/docs/proposals/plexus-scope.md` | `file://$HOME/projects/unitares/docs/proposals/plexus-scope.md` | Canonical Plexus boundary | This document; one holder at a time for edits |
| `repo://unitares/docs/proposals/surface-lease-plane-v0.md` | `file://$HOME/projects/unitares/docs/proposals/surface-lease-plane-v0.md` | Lease-plane contract/RFC | Existing canonical implementation contract |
| `repo://unitares/docs/proposals/surface-lease-plane-phase-a-plan.md` | `file://$HOME/projects/unitares/docs/proposals/surface-lease-plane-phase-a-plan.md` | Phase A staging plan | Implementation sequencing |
| `repo://unitares/docs/ontology/beam-coordination-kernel.md` | `file://$HOME/projects/unitares/docs/ontology/beam-coordination-kernel.md` | Ontology/integration framing | BEAM sidecar role and non-goals |
| `repo://unitares/src/lease_plane/<file>` | `file://$HOME/projects/unitares/src/lease_plane/<file>` | Python client/advisory contract | Choose the exact file before acquiring |
| `repo://unitares/elixir/lease_plane/<file>` | `file://$HOME/projects/unitares/elixir/lease_plane/<file>` | BEAM implementation | Choose the exact file before acquiring |
| `repo://unitares/db/postgres/migrations/<lease-plane-migration>.sql` | `file://$HOME/projects/unitares/db/postgres/migrations/<lease-plane-migration>.sql` | Durable lease schema | Migrations are high-risk surfaces; use explicit exact-path claims |

Do not generalize this table into a fleet-wide taxonomy until manual use shows which surfaces actually collide.

## Agent checklist

Before mutating a Plexus/lease-plane surface, an agent should answer:

1. Is this a read-only review? If yes, no exclusive lease is needed.
2. What exact surface will I mutate?
3. Is the surface already claimed?
4. What is my intent in one sentence?
5. What is my TTL?
6. What evidence reference lets another agent reconstruct why I claimed it?
7. How will I release or hand off if interrupted?
8. Am I trying to put KG, identity, dialectic, chat, or project management into Plexus? If yes, stop.

## Expansion path beyond the first slice

Do not expand the service based on elegance. Expand it based on observed coordination pain.

Minimum evidence before expanding beyond the first slice:

1. At least five real claims on shared surfaces, through the service or the fallback protocol.
2. At least one observed blocked/conflict case or a credible near-miss reconstructed from evidence.
3. A reviewer confirms this document still prevents scope creep.
4. The core collision test stays passing: two holders acquire the same exact surface; one succeeds, one receives the RFC-defined `held_by_other` response.
5. Routine lease lifecycle events do not flood KG; only durable lessons are promoted.
6. The implementation remains inside the existing Surface Lease Plane track unless a separate extraction trigger from `beam-coordination-kernel.md` fires.

These are product-discovery gates for expanding Plexus beyond the first slice. They do not replace the Surface Lease Plane RFC's Phase A/B rollout gates, migration gates, or implementation test gates.

## Relationship to durable memory

Plexus events are operational weather. KG entries are sediment.

Use this routing rule:

```text
If it matters for the next 10 minutes → Plexus / TTL lease.
If it matters for audit reconstruction → audit/event log.
If it matters next week → KG.
If agents disagree about meaning → Dialectic first, then KG.
If humans need social visibility → Discord/Slack summary.
```

Plexus should emit enough telemetry for auditability, but it should not write every lifecycle event into KG.

## Stop signs

Pause and request review if a Plexus proposal introduces any of these:

- unbounded free-form messages;
- agent inboxes or presence as v1 requirements;
- identity creation or lineage mutation;
- KG writes as a lease side effect;
- dialectic session control;
- multi-node distribution before single-node semantics are proven;
- dashboard-first implementation;
- broad surface wildcards as service surface IDs or as the default claim shape;
- lease metadata that starts acting like a generic database.

The boundary is small by design. Plexus succeeds if it makes one class of collision boring without becoming the system that decides everything.
