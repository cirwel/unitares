# Substrate portability of the check-in schema — what breaks when the caller isn't a chat assistant

**Status:** v0 — **canaries only.** This PR changes no math. It fixes two
unambiguous defects (an unanchored regex, a mislabeled scope comment) and wires
three observe-only flags so the load-bearing default changes can be decided
against the live distribution instead of by assertion.
**Author:** substrate-portability audit of `ProcessAgentUpdateParams`, 2026-08-10.
**Why now:** the fleet is no longer Claude-only. Lumen (physical sensors), the
BEAM harnesses, and Watcher (local model) all post check-ins through a schema
whose grounding layer was written for a markdown-formatted assistant turn.

---

## The question

Does the check-in schema assume its caller is a Claude-family conversational
agent? For each field: *what produces this on a substrate that isn't one?*

| Field | Default | What produces it on Lumen | Verdict |
|---|---|---|---|
| `response_text` | `None` | a templated status line | **substrate-shaped** |
| `complexity` | **`0.5`** | `estimate_complexity(anima, readings)` | silent default reads as a report |
| `confidence` | `None` | `compute_confidence()` from state deltas | degrades safely |
| `ethical_drift` | **`[0,0,0]`** | `compute_ethical_drift()` from deltas | zeros read as "no drift" |
| `task_type` | `mixed` | no enum member fits a sensor loop | **SWE-shaped enum** |
| `recent_tool_results` | `None` | n/a; kind inferred from `pytest`/`ruff`/`eslint` | **SWE-shaped** |
| `epistemic_class` | `agent_report` | — | the right mechanism, already present |
| `logprobs` | `None` | — ("Absent for Claude") | the one anti-leak field |

The fields are mostly fine — Lumen computes `confidence` and `ethical_drift`
from state deltas, which is *more* grounded than a model asserting them. The
problems are in the layers written before the substrate seam existed.

## What the audit found

### 1. The operational log measures markdown

`analyze_response_text` extracts code fences, markdown list items, blank-line
paragraphs, question marks, and mentions of UNITARES tool names in prose. This
is the half of the dual log that is supposed to be independent of self-report.
It is independent of self-report and *dependent on substrate*.

Measured against the live formulas:

```
LUMEN   healthy → near-dead     derive_complexity spread = 0.0016
CLAUDE  trivial → heavy turn    derive_complexity spread = 0.2701
```

For a fixed-shape template the channel is a constant. `complexity_divergence`
computed against that constant is a template artifact, not miscalibration —
and it flows into `I_input = 1.0 - complexity_divergence`.

**This does not reach an embodied agent's verdict.** `governance_monitor.py`
prefers a caller-published `sensor_eisv` when present (`if sensor_eisv: … else:
behavioral_sensor`), so Lumen's verdict comes from its sensors. The seam works.
The artifact still reaches stored drift and calibration telemetry for every
agent, which is where the corpus for EISV validation comes from.

### 2. An unanchored regex counted decimals as list items

`r'^\s*[-*•]\s+|\d+\.\s+'` — the ordered-item branch sat outside the `^\s*`
group, so any `<digits>. ` sequence mid-sentence matched. Lumen's real status
line (`"Warmth: 0.62. Clarity: 0.71. …"`) scored **6 list items on a string
containing no list**. Fixed; regression test added.

### 3. `E_input` is a constant for everyone, including Claude

`latency_ms` is the wall-clock gap between check-ins, but the `/200` normalizer
was written for tokens-per-second *of generation*. Clearing the `0.3` floor
needs >60 tok/s sustained across the whole idle gap:

```
Lumen  (56 tok / 300s)      0.2 tok/s → 0.300  FLOOR
Claude (150 tok / 20s)      7.5 tok/s → 0.300  FLOOR
Claude (2000 tok / 60s)    33.3 tok/s → 0.300  FLOOR
Claude (2000 tok / 5s)    400.0 tok/s → 1.000
```

`behavioral_sensor` then blends this in at 20%, so it acts as a steady downward
pull on E rather than a measurement. Not fixed here — picking a new divisor
would just be a second made-up constant. Flagged via `E_input_clipped` so the
term can be retired or renormalized against evidence.

### 4. The verification floor is scoped more narrowly than its comment says

`governance_core/verification.py` is commented as applying "regardless of
behavioral confidence because it is self-report-independent." True — it ignores
`confidence`/`complexity`/`ethical_drift`. But every pattern is **English
first-person prose describing an action already taken** (`exfiltrat\w*`,
`they'?ll\s+never\s+know`, `bypass … the safety gate`).

It is structurally incapable of firing for a caller whose `response_text` is a
templated status line or a state digest. Those agents are **unscored**, which
reads downstream exactly like **cleared**. The floor is default-off
(`GOVERNANCE_VERIFICATION_FLOOR=false`, shadow on), so this is caught before the
council-gated enable — but the shadow record now accumulating has a denominator
of "agents that write English", not "agents". Docstring and call-site comment
corrected to say so.

## What this PR does

**Fixed** (unambiguous defects, no judgement call):
- Anchored the ordered-list branch of the list-item regex.
- Corrected the verification floor's scope claim in both the module docstring
  and the `governance_monitor` call site.
- Documented the substrate assumption on `analyze_response_text`.

**Wired, observe-only** (`ContinuityMetrics`, surfaced in
`eisv_telemetry.derivation.substrate_canaries`):

| Canary | Fires when |
|---|---|
| `self_complexity_defaulted` | divergence used the `0.2` stand-in, no real self-report |
| `E_input_clipped` | `E_input` hit a clip bound and carries no signal |
| `continuity_degenerate` | no structure, no tools, no questions — text features can't move |

**Deliberately not changed.** Each of these is a fleet-wide behavioral change
and each is gated on its canary producing data first:

1. `complexity` default `0.5` → `None`. Today a caller that omits it is
   indistinguishable from one reporting 0.5. Blocked on: what fraction of live
   check-ins set `self_complexity_defaulted`.
2. `ethical_drift` default `[0,0,0]` → `None`. Zeros currently read as "no
   drift" — a fail-toward-healthy default on a governance input.
3. Retiring or renormalizing the `E_input` blend. Blocked on the
   `E_input_clipped` rate.

## Decision points for review

- Are the three canaries the right cut, or is there a fourth (e.g. `task_type`
  defaulted to `mixed` because no enum member fits the caller)?
- Should `continuity_degenerate` suppress the `complexity_divergence` write
  entirely rather than annotate it? That is a math change, hence not here.
- `task_type`'s enum is software-engineering shaped
  (`bugfix`/`testing`/`deployment`/…). A sensor loop has no member. Worth a
  `substrate` or `sensing` member, or is `introspection` the intended catch-all?

## What was not audited

The BEAM/orchestrator check-in path. The table's "what produces this" column is
filled for Claude and Lumen only, both verified empirically. Given the known
REST-attestation gap, that path is the next place to look.
