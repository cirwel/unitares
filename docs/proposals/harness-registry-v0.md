# Harness registry — an authoritative catalog of harness *types* (not identity)

**Status:** v0 design — **NOT a committed change; DO NOT BUILD YET.** Design-first RFC.
For the identity/ontology owners. The *descriptive census* (PR #1153) is the evidence
arm; this is the decision it informs.
**Author:** follow-on to the harness census, 2026-06-28.
**Build-trigger:** census evidence crosses the promotion thresholds in §6 — not before.

> This proposal sits on the **identity/ontology coupled surface** (see the shared
> contract). It promotes a deliberately-deferred field (`harness_id`/`harness_type`,
> `docs/ontology/plan.md` Track D / `harness-substrate-plurality.md` §"`harness_id`
> granularity"). It does **not** touch the identity write-gate, and it explicitly keeps
> harness **out** of the identity/auth path.

---

## The question this answers (and the one it refuses)

**Answers:** "What harnesses exist, what kind of thing is each, and what may we assume
about them?" Today the answer is scattered: `harness_id`/`harness_type` are self-declared
labels on S22 write-context (`provenance_context` / `s22_context`), canonicalized only
by a best-effort alias map (`normalize_s22_harness`). There is no authoritative catalog —
a typo'd or novel `harness_type` is indistinguishable from a real one.

**Refuses:** "Who is this agent?" Harness is **body/runtime, not selfhood**
(`harness-substrate-plurality.md`: the 2026-04-30 incident showed *same UUID, same
harness, same transport, different situated reach*). The registry must never become an
identity or auth gate. It informs calibration weighting, affordance expectations, and
routing — not accountability for *who* acted. That stays with the UUID/identity layer.

---

## The core design decision: catalog *types*, observe *instances*

`harness-substrate-plurality.md` flags the open question directly: *"A harness has both a
type and an instance. `hermes` as a type is too coarse to distinguish Hermes CLI, Hermes
Discord gateway, Hermes profile, and Hermes MCP host behavior."* This proposal resolves
it by **splitting authority from observation**:

| | Harness **type** | Harness **instance** (`harness_id`) |
|---|---|---|
| What | a kind of body/runtime (`claude-code`, `codex-cli`, `hermes-cli`, `hermes-discord-gateway`, `hermes-mcp-host`) | a concrete running body (`hermes-discord-gateway@host-3`) |
| Source | **declared** — a reviewed, versioned catalog | **observed** — telemetry, stays in the census |
| Authority | authoritative (a closed, curated set) | **non-authoritative**, as today |
| Cardinality | small, slow-changing | open, churny |

So the *registry* is a small declared catalog of **types**. Instances remain exactly what
they are now — self-declared observations the **census** (PR #1153) rolls up. This keeps
the authoritative surface tiny and reviewable, and it honors the deferral: we are not
promoting `harness_id` (instance) to first-class — we are giving `harness_type` a curated
vocabulary and leaving instances observational until the evidence says otherwise (§6).

---

## Shape (v0 — illustrative, not committed)

**File-backed, not a DB table.** Following the research-registry precedent (file-backed by
design, no migration) and the DB caution in `CLAUDE.md` ("do not create additional
migration layers"), the catalog is a versioned in-repo declarative file — reviewable in a
PR, diffable, no schema migration:

```yaml
# config/harness_catalog.yaml   (illustrative)
schema: harness_catalog.v0
harnesses:
  - id: claude-code            # canonical type id (matches normalize_s22_harness output)
    display_name: Claude Code
    kind: cli                  # cli | gateway | mcp_host | profile | substrate
    aliases: [claude, claude-cli, anthropic-claude-code]
    notes: interactive + headless coding harness
    status: active             # active | deprecated | observed_only
  - id: hermes-discord-gateway
    display_name: Hermes (Discord gateway)
    kind: gateway
    locus_dimensions: [guild_id, channel_id, thread_id]   # what situates an instance
    status: active
```

- `normalize_s22_harness`'s alias map **moves into the catalog's `aliases`** (single source
  of truth instead of a hardcoded dict).
- `kind` is the type-vs-instance bridge: it declares *what dimensions situate an instance*
  (a Discord gateway is situated by `locus`; a CLI by `episode_id`/`invocation_id`).
- `status: observed_only` is the honest landing spot for a harness the census sees but that
  hasn't been curated yet — it exists in telemetry, not yet authoritative.

**Consumers (read-only, additive):**
- the census validates observed `harness_type`s against the catalog → surfaces
  `uncatalogued` harnesses (the labelling/typo gap, today invisible);
- calibration *may* weight by harness `kind` (a gated pre-check from a gateway carries
  different evidence than an inner-loop CLI tool call — already noted in
  `harness-substrate-plurality.md` re `governance_mode`);
- **never** identity/auth.

---

## What it is NOT

- **Not identity.** No write is accepted or rejected because of harness. No tier, no
  assurance, no accountability flows from it. (`agent_auth` / identity-step untouched.)
- **Not instance tracking.** Instances (`harness_id`) stay observational in the census.
  The registry catalogs *types*.
- **Not a new DB/migration layer.** A reviewed file, not a table.
- **Not the cross-harness event envelope** (`harness-event-safety-policy-v0.md`) — that's
  the *transport* of provenance; this is the *vocabulary* for one of its fields.

---

## 6. Promotion criteria — gated on census evidence (the build-trigger)

This is design-first precisely because the evidence isn't in yet. Concrete go/no-go,
read straight off the census (PR #1153) + `s22_candidate_envelope_coverage.py`:

1. **Type stability** — the census's `distinct_harnesses` set is small and stable across
   windows, and `unattributed_entries` is a small fraction (most writes carry *some*
   harness label). If many writes are unattributed, curate the label-emission first; a
   catalog over sparse labels is theatre.
2. **Per-type volume** — each catalogued type has enough entries to be worth curating (not
   a one-off typo).
3. **Instance question stays deferred** until the census's `instance_label_ratio`
   (added in PR #1153) is high across types — i.e. `harness_id` is densely and stably
   emitted. Until then, `harness_id` is observed-only, exactly as `plan.md` D3 holds.

Exit criterion (matches `plan.md` Track D): **one evidence-backed promotion decision** —
"here are the N types the census actually shows, with these volumes and label ratios;
curate this catalog" — plus a fresh `s22_candidate_envelope_coverage` reading recorded.

---

## Open questions (for owner review)

- **Catalog home & format.** `config/harness_catalog.yaml` vs `docs/ontology/` vs a small
  Python module. File-backed is the constraint; exact location is owners' call.
- **Who curates, and how is drift caught?** A `--check` mode (census `harness_type`s ⊆
  catalog ∪ `observed_only`) would catch un-catalogued harnesses — but that's another CI
  gate; weigh against the gate-fatigue cost.
- **`kind` taxonomy.** `cli | gateway | mcp_host | profile | substrate` is a first cut;
  needs the owners' read against the real fleet.
- **Interaction with the substrate identity pattern.** Persistent/substrate agents have a
  dedicated identity pattern; does a substrate harness `kind` interact with it? Likely no
  (identity ≠ harness), but state it explicitly.
- **Capability/affordance modeling is OUT.** Tempting to attach capabilities to a harness
  type, but reach is per-*instance*-per-*locus* (`affordance_state`, the 2026-04-30
  incident), not per-type. Defer to the `affordance_state` design pass; the catalog
  carries at most `locus_dimensions` hints.

## Relation to neighboring work

- [`harness-census` (PR #1153)](https://github.com/cirwel/unitares/pull/1153) — the evidence
  arm; this proposal's go/no-go reads off its `situating_metadata_ratio` /
  `instance_label_ratio` / `unattributed_entries`.
- [`docs/ontology/harness-substrate-plurality.md`](../ontology/harness-substrate-plurality.md)
  — the type-vs-instance open question this resolves; the body/runtime ≠ identity stance.
- [`docs/ontology/plan.md`](../ontology/plan.md) Track D (D3/D4) — the promotion-evidence
  gate this conforms to.
- [`harness-event-safety-policy-v0.md`](harness-event-safety-policy-v0.md) — the envelope
  that *carries* `harness_type`; orthogonal (transport vs vocabulary).
