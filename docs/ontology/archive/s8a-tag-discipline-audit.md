# S8a — Tag-discipline audit

**Date:** 2026-04-23
**Scope:** Re-measure class-tag coverage (A3 said 96% untagged as of 2026-04-21), identify the structural write-path gap, recommend a fix.
**Stance:** Descriptive. No code changes in this pass — findings only.
**Unblocks:** S6 session-like threshold recalibration (blocked because the class partition is degenerate); S8b archival backfill (blocked by missing tags on archived records).

## Measurement (2026-04-23)

| Cohort | Total | With class tag (`ephemeral` / `persistent` / `embodied`) | Untagged |
|---|---:|---:|---:|
| Active identities | 172 | 6 | **166 (96.5%)** |
| Archived identities | 3182 | 2 | **3180 (99.9%)** |

Tag-set distribution among the 6 tagged active agents (all residents):

```
2  ["persistent", "autonomous"]                                          # Watcher x2
1  ["persistent", "autonomous", "cadence.10min"]                         # Sentinel
1  ["persistent", "autonomous", "cadence.30min"]                         # Vigil
1  ["persistent"]                                                        # Watcher_7bf970d4 (partial)
1  ["pinned", ..., "embodied", "lumen", "persistent", "creature", ...]   # Lumen
```

Expected class under `src/grounding/class_indicator.py:classify_agent`:

| Class | Count |
|---|---:|
| `default` (no class signal) | 166 |
| `resident_persistent` | 5 |
| Partial / other | 1 |
| `ephemeral` | **0** |

**No agent in the production fleet carries the `ephemeral` tag** — despite ontology v2 treating ephemeral-by-construction process-instances as the normal case. The class enumeration in `class_indicator.py` includes `ephemeral` but no production write path stamps it.

Lineage signal among the 166 untagged actives:

| Lineage | Count |
|---|---:|
| `parent_agent_id` declared | 19 |
| No declared parent | 147 |
| `spawn_reason` declared (any) | 0 |

A3's 2026-04-21 numbers held: coverage has not shifted; the structural gap is unchanged.

## Root cause

**There is exactly one production write path for class tags, and it only fires for resident SDK agents.**

- `agents/sdk/src/unitares_sdk/agent.py:40` defines `RESIDENT_TAGS = ["persistent", "autonomous"]`.
- `agents/sdk/src/unitares_sdk/agent.py:247-254` calls `update_agent_metadata` with `tags=RESIDENT_TAGS` — but only inside the SDK branch gated on `persistent=True`. The six subclasses passing `persistent=True` are Vigil / Sentinel / Watcher / Steward / Chronicler / Lumen. Nothing else.
- The target handler (`src/mcp_handlers/lifecycle/mutation.py:31 handle_update_agent_metadata`) accepts `tags` via a general metadata-update entrypoint (`update_agent_metadata(tags=[...])`). It works as designed.
- `src/mcp_handlers/schemas/lifecycle.py:36` declares an `add_tag` / `remove_tag` action schema for an agent-lifecycle endpoint — grep turns up **no handler consuming this action**. The schema is orphaned; any caller invoking `add_tag` would hit a no-op.
- The onboard path (`src/mcp_handlers/identity/handlers.py` — the `onboard` tool) creates identities with `metadata={}`. No default tag. No tag inference from `model_type`, `client_hint`, `parent_agent_id`, or any other onboard input.

**Consequence:** every agent that isn't a resident SDK subclass onboards with no class tag, stays that way for its lifetime, and gets classified as `default` by every downstream consumer (class-conditional calibration, trust-tier routing, archive-orphan-sweep, loop-detection exemptions, dashboard filters).

The ontology documents (identity.md §Implications, paper §4 Heterogeneous Agent Fleets) assume the four-class partition is populated. In production, one class (`default`) holds 96.5% of the fleet and three (`ephemeral`, `embodied`, `resident_persistent`) collectively hold 3.5%. The partition exists in code and documentation; it does not exist in data.

## Why it matters

1. **S6 threshold recalibration is degenerate.** The S6 design (Option B routing, shipped PR #107/#112) routes substrate-earned agents through the R4 three-condition check and session-like agents through `compute_trust_tier`. But "session-like" threshold recalibration — the remaining S6 work — needs per-class empirical partitions. With 96.5% in `default`, the only partition available is `default` vs. `resident_persistent`, and `default` is a bucket, not a class.

2. **S8b archival backfill is blocked.** 3180 archived identities carry no class tag. Any retroactive analysis (lifetime distributions per class, drift events per class, calibration curves per class) against the historical record has the same partition problem the live fleet has. Backfilling requires a rule — and the rule has to come from somewhere.

3. **R4 substrate-earned verification is under-signalled.** 19 active untagged agents declared `parent_agent_id` at onboard — candidates for R4's substrate commitment. Zero have `spawn_reason` recorded. The R4 three-condition check can still run, but two of its three input signals (class tag, spawn_reason) are empty for most candidates.

4. **`ephemeral` class is empty by construction.** The class name exists (`CLASS_EPHEMERAL` at `src/grounding/class_indicator.py:24`), the routing exists (`if "ephemeral" in tags: return CLASS_EPHEMERAL` at line 51), and zero agents carry the tag. The paper §4 partition has a hole the runtime cannot detect.

## What would fix it

Three structural options, listed in increasing invasiveness:

### Option 1 — Default-stamp at onboard

Add to the onboard handler: if `force_new=true` and no tags in incoming metadata, stamp one default class tag based on onboard inputs. Candidate rule:

- `persistent=true` flag (SDK path) → `persistent` + `autonomous` (current SDK behavior, just moved server-side)
- Resident label (Lumen / Vigil / Sentinel / Watcher / Steward / Chronicler) → as above, plus resident-specific tags
- Everything else → `ephemeral`

**Pros:**
- Closes 96.5% of the gap at a single write site.
- Makes the paper §4 partition populated — every onboard produces a class-tagged record.
- Backward-compatible for residents (the SDK-stamp path becomes redundant but still works).

**Cons:**
- "Everything else → ephemeral" over-assigns. Long-running Claude Code IDE sessions, Codex desktop sessions, and operator-driven agents may last hours or days and aren't morally ephemeral. They still deserve a separate class; they just don't have one in the current taxonomy.
- Locks in the classification at onboard time based on signals (label pattern, `persistent` flag) that don't capture behavioral class. A "session" tag (distinct from `ephemeral` and `persistent`) would be more honest — requires adding a class.

### Option 2 — Observer-tag promotion (soft classification)

Leave onboard unchanged. Add a sweep agent (launchd cron or in-process, like Steward) that periodically promotes identities based on observed behavior:

- Cold promote: after 24 hours of no updates → `ephemeral` (and archive soon after)
- Warm promote: after N observations spanning >7 days → `session_like` (new class, needs adding) or `persistent` (if update cadence is also regular)
- Cold residency: declared `parent_agent_id` chain reaching a known resident → `resident_lineage` (new class, R4-aligned)

**Pros:**
- Classification is earned, not assumed. Matches v2's behaviorist stance.
- Doesn't require deciding a taxonomy up front — observe first, name classes from the distribution.
- Reversible. A bad rule doesn't poison the onboard write path; it just re-tags.

**Cons:**
- Classification lag. Agents spend their first hours/days in `default` and confuse any downstream consumer that expects a class.
- Adds a moving part (sweep agent) with its own calibration.
- Needs a source-of-truth convention for class precedence when the sweep and SDK disagree.

### Option 3 — Require class at onboard

Reject onboard calls that don't carry a class tag. Forces callers (SDK, plugin, Codex harness, Claude Code hooks, dispatch) to declare intent.

**Pros:**
- Data is authoritative — every agent knows what it is.
- Forces the taxonomy to be revisited at every integration surface, catching stale assumptions.

**Cons:**
- Invasive. Breaks every current external caller. Requires coordinated rollout across SDK, plugin, Codex harness, Claude Code hooks, Discord dispatch.
- Doesn't solve the retroactive problem (3180 archived records still untagged).
- The "what am I" decision is often not the agent's to make — a subagent dispatched from a parent agent classifies differently from a standalone CLI session of the same model.

## Recommendation

**Option 1 + partial Option 2, phased.**

Phase 1 (onboard default-stamp): server-side, single write point. Rule set:

- Resident label match → existing SDK tags (move SDK logic to server, deprecate SDK branch)
- `parent_agent_id` declared and reaches a known resident in ≤3 hops → `resident_lineage` (new class; see note below)
- Everything else → `ephemeral`

Phase 2 (reclassification sweep): a small launchd or Steward-embedded sweep that:

- Promotes `ephemeral` → a long-lived class (name TBD; `session_like` is the current working term) after N observations over D days with no declared parent
- Leaves `resident_*` classes alone (those are SDK-stamped or lineage-declared and authoritative)
- Backfills archived identities on a one-shot pass using the same rules

**Open taxonomic question:** the current four classes (`default`, `ephemeral`, `resident_persistent`, `embodied`) don't have a class name for "interactive session bounded by a human operator's working day." `ephemeral` fits literally (the process will die) but misclassifies the calibration pool (these agents accumulate far more observations than a dispatched subagent). Adding a `session_like` class to `grounding/class_indicator.py` is a small change; the class-conditional scale maps in `config/governance_config.py` need a new entry.

**Open engineering question:** should the onboard default-stamp live in the `onboard` handler directly, or in a new `classify_on_onboard` helper that the handler calls? The helper split preserves testability and keeps the class-inference rules in one place; the ontology-plan pattern favors this.

## Gaps worth noting (not S8a scope)

- `add_tag` / `remove_tag` schema at `src/mcp_handlers/schemas/lifecycle.py:36` has no consumer. Either delete the schema or wire it to a handler. Orphaned code that looks like it works is worse than absent code.
- `spawn_reason` is declarable at onboard but **zero** of the 19 lineage-declared active agents recorded one. Either the plumbing drops it or no caller passes it. Worth a separate look during S11-adjacent plumbing audit.
- `structured_agent_id` and `label` carry class-adjacent information (e.g. `Claude_Sonnet_4_6_*` in label indicates a session-model lineage) that the current `classify_agent` doesn't use. Not a fix — but informative for the Phase-1 rule set.

## Unblocking table

| Downstream item | Unblocked after | Notes |
|---|---|---|
| S6 session-like threshold recalibration | Phase 1 + a week of data | Once `ephemeral` is populated, empirical per-class thresholds become computable |
| S8b archival backfill | Phase 2 one-shot pass | Rule-based backfill is cheap once the live rules are agreed |
| R4 substrate-earned audit | Phase 1 + `spawn_reason` plumbing fix | Substrate signals become measurable when lineage + class + spawn_reason all populate |
| Dashboard class filters | Phase 1 | 96.5% "default" is not a useful filter today |

## Operator decision needed

- **Pick Phase-1 rule set.** The three-branch rule above is a starting point, not a ruling.
- **Ratify or veto the `session_like` class addition.** Requires a scale-map entry in `governance_config.py`.
- **Dispatch-or-hold Phase 2.** Phase 1 is valuable standalone; Phase 2 can wait until Phase 1 runs a week.
