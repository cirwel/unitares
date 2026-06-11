# UNITARES Trust Contract

**Status:** Draft v0.2 — 2026-06-11
**Provenance:** v0.1 drafted 2026-06-10 by an external claude.ai session reviewing the *live* system through the MCP connector (every §6 row is a value the running server returned, not a code-read). v0.2 grounds each violation against the repo: row statuses, the grounding log (§9), and §7 enforcement statuses added by the in-repo session that shipped the fixes. The contract text itself is otherwise the reviewer's.
**Scope:** Defines what the system guarantees, what it does not, and what honest failure looks like. Any endpoint behavior that violates this document is a bug, regardless of whether the code "works."

-----

## 1. The core guarantee

> Every value UNITARES emits carries provenance: **measured**, **derived**, **prior/default**, or **unknown**. The system never presents a prior with the confidence of an observation.

This is the data-layer form of the founding principle: *build nothing that appears more alive than it is.* The system must not appear more **knowing** than it is.

## 2. Provenance taxonomy

Every emitted field is one of:

|Class     |Meaning                                     |Emission rule                                                       |
|----------|--------------------------------------------|--------------------------------------------------------------------|
|`measured`|Direct observation from a check-in or sensor|Emit with timestamp + observation count                             |
|`derived` |Computed from measured values               |Emit with derivation source + inputs' min provenance                |
|`prior`   |Default, flat prior, or ODE fallback        |Emit only with explicit `source` label and a confidence/warmup block|
|`unknown` |Insufficient data                           |Emit `null` + reason. Never substitute a prior silently             |

A composite value (e.g., `summary`, `state.health`) inherits the **weakest** provenance of its inputs. A summary built on priors is a prior and must say so — or not be emitted at all.

## 3. Guarantees (the hardened core)

1. **State integrity.** The state recorded at check-in is the state reported at read. No transformation between write and read without a `derived` label.
2. **Honest uninitialized behavior.** An agent with `history_size: 0` produces no verdicts, no health labels, no trajectory claims, no guidance prose. Allowed outputs: identity, warmup status, `null`s with reasons, and "submit a check-in to activate."
3. **Scope honesty.** Agent-scoped responses contain only agent-scoped data. Fleet-level statistics (calibration accuracy, basin distributions) appear only in fleet endpoints or under an explicit `scope: fleet` key.
4. **Single source per field.** A field name means one thing, computed in one place. `stats` and `list` (or any two endpoints) reporting different values under the same name is a contract violation, not a quirk.
5. **Read-only means read-only.** No read endpoint creates, mutates, or auto-mints identity. Identity creation is an explicit act (`onboard`).
6. **First-class ignorance.** `"unknown"`, `"uninitialized"`, `"insufficient_data"` are designed outputs with stable schemas — not error states, not gaps papered over by defaults.

## 4. Non-guarantees (stated plainly)

- **Predictive validity of EISV is a research claim, not a system guarantee.** The system guarantees honest computation and labeling of EISV, not that EISV predicts agent failure in your deployment.
- **Experimental surfaces** (dialectic, semantic search, extended observability) carry `tier: experimental` and may change, lag, or be wrong. They are excluded from the core contract until promoted.
- **No availability SLA.** This is research infrastructure.
- **Calibration figures describe the fleet they were fitted on.** They do not transfer to new deployments without re-baselining.

## 5. Honest-failure schemas

Each core endpoint must define its ignorance shape. Examples:

```jsonc
// get_governance_metrics, uninitialized agent
{
  "status": "uninitialized",
  "history_size": 0,
  "verdict": null,            // not "moderate"
  "summary": null,            // not "moderate | building_alone | high basin"
  "guidance": null,           // not "pattern may be shifting"
  "stability": { "status": "insufficient_data", "min_observations": 30 },
  "behavioral_eisv": { "confidence": 0.0, "warmup": { ... } },
  "primary_eisv": { "values": {...}, "source": "ode_fallback",
                    "interpretation": "fallback estimate, not observation" }
}
```

The currently shipping `primary_eisv_source` + `verdict_source_meta` pattern is the model. Extend it to **every** composite field, especially `summary`, `state`, `stability`, and `calibration_feedback`.

> **Implementation note (v0.2).** The shipped ignorance shape (PR #605) deviates from the example above in one deliberate way: `summary` emits the self-describing string `"uninitialized | no observations yet"` and `verdict` emits the glossary-wrapped `"uninitialized"` rather than bare `null`s. Same §3.2 honesty (warmup status is an allowed output); more legible to a cold caller than a null. If a consumer ever needs the strict-null form, that is a schema-versioning decision, not a re-litigation of the principle.

## 6. Known violations as of v0.1 (live probe, 2026-06-10) — grounded v0.2

|#|Violation                                                                                           |Contract clause                 |Severity|Status (2026-06-11)|
|-|----------------------------------------------------------------------------------------------------|--------------------------------|--------|---------------------|
|1|`summary` / `state.health` / `trajectory` emit confident labels at `history_size: 0`                |§3.2, §2                        |High    |**FIXED** — PR #605: `interpret_state` no longer runs for uninitialized agents; pending block replaces it|
|2|`guidance: "Pattern may be shifting"` with zero observations                                        |§3.2                            |High    |**FIXED** — PR #605: source traced to `_generate_guidance`'s borderline branch firing on seed values; gated|
|3|`stability.stable: true` + 15-decimal alpha on no history, unlabeled                                |§2 (prior presented as measured)|High    |**FIXED** — PR #605: pending block; also nulled inside nested `ode_diagnostics` (review fold)|
|4|`calibration_feedback.system_accuracy` (fleet) inside agent-scoped response, unscoped               |§3.3                            |Medium  |**FIXED** — PR #605: unconditional `scope: "fleet"` label (the "System-wide" disclaimer was cache-gated and usually absent); block omitted entirely for uninitialized agents|
|5|`get_governance_metrics` auto-minted identity `auto_20260610_…` via ip/ua fingerprint on a read call|§3.5                            |High    |**CONFIRMED / NARROWED** — mint is real and per-call (3 cold probes → 3 distinct `auto_*` identities) but **in-memory only: zero `core.identities` rows written** (count stayed exactly 3,934 through all probes). The ghost-ROW correlation is refuted — the 3,934 population is historical (April-era `mcp_*` poller mints predating the reserved-prefix gate). The residual violation is per-call in-process monitor/metadata growth; fix planned: per-tool no-mint middleware flag mirroring `requires_identity` (identity surface, council-gated). The §7 read-purity experiment was run as designed and is what separated row-factory from in-memory reality|
|6|EISV values duplicated across 4 top-level shapes (`E/I/S/V`, `eisv`, `ode`, `primary_eisv`)         |§3.4                            |Medium  |**OPEN** — real; consolidation needs a deprecation pass over consumers before removal|
|7|Redis keyspace hit rate 1.4% (113k hits / 7.9M misses)                                              |— (operational)                 |**DIAGNOSED, not a defect** — 651 keys total; the in-memory `_session_identities` layer absorbs hits first, so Redis (layer 2) sees mostly probes for keys that legitimately don't exist (3-4 scoped pin candidates per argument-less call + unbound session keys, amplified by resident polling). Negative-caching is a future optimization|

## 7. Enforcement

- **Contract tests, not spot checks.** A test suite that onboards a fresh agent and asserts the §5 ignorance schemas, byte for byte. Run in CI. Violations 1–3 above would have been caught at design time by a single test: *"no endpoint emits a qualitative label when history_size == 0."*
  *Status: partially shipped — `tests/test_zero_observation_honesty.py` (PR #605) pins the §5 shape for `get_governance_metrics` across full/lite/standard verbosities plus an initialized-agent regression guard. Other endpoints (`observe`, `agent` status) not yet covered.*
- **Provenance linting.** A response-schema validator that rejects any emitted numeric/label field lacking a provenance class. Mechanical, not judgment-based.
  *Status: not built.*
- **Cross-endpoint consistency test.** For every field name appearing in ≥2 endpoints, assert equality from a single fixture state.
  *Status: not built (the §6.6 EISV-quadruplication fix is its prerequisite).*
- **Read-purity test.** Snapshot identity count, call every read endpoint unbound, assert identity count unchanged.
  *Status: procedure validated manually 2026-06-10 (it produced the §6.5 narrowing). CI form lands with the no-mint middleware PR — the row-level assertion already holds; the in-memory assertion (monitor-cache size unchanged) is the part the fix must make true.*

## 8. Promotion path for experimental surfaces

An experimental feature joins the core contract only when it: (a) passes provenance linting, (b) has a defined ignorance schema, (c) has contract tests, and (d) has run ≥30 days without a contract violation. Until then it ships behind `tier: experimental` and is absent from default responses.

## 9. Grounding log (v0.2)

- **2026-06-10** — v0.1 drafted from a live probe by an external reviewing session (claude.ai connector). All seven §6 rows reproduced against the running server by the in-repo session before any fix: three cold REST probes with a fresh UA confirmed rows 1–3 verbatim, row 4's cache-gated disclaimer, and row 5's per-call in-memory mint with zero row writes.
- **2026-06-10/11** — PR #605 (zero-observation honesty + fleet scope label) merged; rows 1–4 closed. Single-lane adversarial review caught the nested `ode_diagnostics` seed-value leak; folded.
- **Related, same week:** PR #601 (lineage role-family envelope — 47/47 false rejections), PR #603 (mirror novelty gate + proxy-basis disclosure + phi surfacing), PR #604 (subagent onboards no longer displace the driver's fingerprint pin — the read-resolution mis-attribution class). The contract's §1 sentence — *never present a prior with the confidence of an observation* — is the same principle all four enforce on different surfaces.

-----

*Violations of this document outrank feature work. A governance system that misreports its own knowledge state has negative value: it doesn't merely fail to govern — it manufactures false confidence in the agents it observes.*
