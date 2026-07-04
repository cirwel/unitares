# Stateless-MCP consumer-chat UX dogfood — worked report

**Recorded:** July 4, 2026
**Status:** Dogfood UX report from a **first-contact consumer chat** driving the full
loop (`onboard` → `sync_state` → `record_result`) over stateless MCP transport. Logs
what is remediated in the live system and scopes the residual friction. Two long-tracked
findings — continuity-token confusion and self-report laundering — show visible
remediation here; one known gap (verdict confidence not scaled by corroboration grade)
is partially addressed by this artifact's accompanying code change.
**Surface:** onboarding + verdict presentation — `src/mcp_handlers/identity/`,
`src/mcp_handlers/observability/outcome_events.py`, `src/governance_glossary.py`
(`explain_verdict`), `src/mcp_handlers/response_formatter.py`.
**Captured against:** stateless MCP transport, model `claude-opus-4.8`, consumer chat
(no persistent adapter threading `client_session_id`).

> Read [`docs/operations/self-report-verdict-dependence-2026-06-28.md`](self-report-verdict-dependence-2026-06-28.md)
> and [`docs/proposals/verification-weighted-verdict-v0.md`](../proposals/verification-weighted-verdict-v0.md)
> first — this report is downstream of both. EISV/risk/coherence are **policy inputs,
> not the actuator**; this is about *how confidently the verdict is worded*, not about
> the verdict changing a decision.

---

## What worked, first try

### 1. Continuity-token path is fixed in practice
`onboard` returned a `how_to_strengthen` field naming exactly what to echo. One
`sync_state` later, identity assurance moved **weak (0.35) → strong (1.0)** over
stateless transport. The `baseline_note` — *"weak is expected for a fresh mint, not a
deficiency"* — is a genuinely good affordance: it pre-empts the exact misreading earlier
sessions hit (treating a fresh-mint weak score as a fault).

### 2. Self-report laundering has teeth on the labeling side
A `record_result` claiming score `0.8` came back graded
`corroboration_grade: claim_only`, `evidence_weight: 0.1`, `claim_risk: high`. The server
did **not** verify the claimed number — but it **refused to launder it**. That is the
honest intermediate step before `server_observation` lands: label the claim as a claim.

### 3. Prediction binding and measurement-role epistemics are clean
`prediction_id` → `record_result` bound cleanly (`prediction_binding: registry`), and the
`measurement_role` note (*"EISV are policy inputs, not the actuator"*) is correct
epistemics surfaced at the right place.

**Net on the two long-hammered findings:** token confusion and self-report laundering
both show visible remediation in the live system.

---

## Friction (falsifiable observations)

### F1 — `force_new=true` mints a fresh identity but infers thread membership
`force_new=true` correctly minted a **fresh identity**, then silently attached the caller
as **node 57 in an existing thread** via IP/UA inference — a lineage the caller never
chose. **Identity is caller-proven now; thread membership is not.** The transport
fingerprint that (correctly, as a weak fallback) pins identity under co-residency is also
being read as a thread-membership signal, which is a different and unchosen claim.
*Repro:* mint with `force_new=true` from a shared-egress consumer chat with no
`client_session_id`; inspect the assigned thread/lineage node against what the caller
declared (nothing). *Expected honest behavior:* a fresh mint with no declared
`parent_agent_id` should not acquire thread membership by inference — or should surface
the inference as provisional, the same way identity assurance is surfaced as weak.

### F2 — Verdict wording outruns its evidence (partially addressed here)
*"Behavioral assessment: low risk"* was issued on **check-in #1**,
`primary_eisv_source: ode_fallback`, behavioral confidence **0.1**, warmup **1/30**. The
verdict text reads more confident than the system actually is in that regime: pre-warmup
the behavioral verdict is the **ODE cold-start prior**, not a warm measurement (see
`self-report-verdict-dependence-2026-06-28.md`, *Reconciliation*). The verdict should
carry its own evidence weight — *"provisionally safe, claim-only"* — exactly as the
`outcome_event` path already grades a claimed score (`corroboration_grade: claim_only`).

**Addressed (presentation-only) in the accompanying change:** `explain_verdict` now
takes an `evidence_source` and, when it is `ode_fallback`, grades a *behavioral* verdict
**provisional** — appending a qualifier to the meaning and attaching an `evidence` block
(`grade: provisional`, `corroboration: claim_only`, `basis: ode_fallback`). Wired at the
agent-facing check-in surfaces (mirror, compact, self-observation). This changes **no**
risk number, decision, or enforcement — it only stops the *wording* from outrunning the
evidence, mirroring the outcome-event labeling philosophy. The deeper axis — making the
*risk math* verification-weighted — remains the reserved v2 work tracked in
[`verification-weighted-verdict-v0.md`](../proposals/verification-weighted-verdict-v0.md)
and [`continuous-verdict-blending-v0.md`](../proposals/continuous-verdict-blending-v0.md).

> **Gate guardrail (read before extending).** This grading is **presentation-only** and
> is *not* council-gated: it sits in the same class as the outcome-event corroboration
> labeling (`claim_only` / `evidence_weight`), which grades the confidence of a surfaced
> claim without touching the actuator. The council gate protects `resolve_verdict_risk`,
> the one-sided blend invariant, and *enabling* `GOVERNANCE_VERIFICATION_FLOOR` — i.e.
> anything that can change **what verdict/decision/enforcement fires**. The moment an
> `evidence: provisional` grade is allowed to gate enforcement, de-escalate risk, or
> otherwise feed the decision, it crosses into actuator territory and inherits that gate.
> Keep it a label.

### F3 — Continuity token is context-window-hostile for LLM callers
The ~340-char `continuity_token` echoed on **every** call is expensive for an LLM caller
holding it in context. A short opaque handle (server-side lookup) would be kinder to the
consumer without weakening the proof. *Note:* this trades a stateless self-describing
token for a server-side indirection; the retirement of stateful stores
(`docs/proposals/redis-retirement-v0.md`) is the relevant constraint on where the handle
would resolve.

### F4 — Cold param surface is intimidating on first contact
`onboard` exposes ~19 params, and descriptions assume the reader has internalized
`identity.md` (references like `S13`, `S22 H5`, `PATH 0`). Fine for a known fleet;
high-friction for a first-contact agent. A minimal first-contact path (the
`start_session(force_new=true)` two-field contract from the `Strict Identity, Simple
Contract` block) is documented but not what the raw param surface leads with.

### F5 — Mirror bug not reproducible from a benign session
Mirror returned *"no actionable signals"* — **correct here**, because state was genuinely
steady. So this run could not reproduce the prior mirror bug: that test needs an
**induced at-risk state**, not a benign one. Noted so a future repro attempt starts from
a deliberately perturbed EISV rather than a calm session.

---

## Interpretation — what this establishes

**Establishes:**
- The continuity-token remediation holds end-to-end over stateless transport from a
  first-contact consumer chat (weak → strong in one echo).
- The self-report labeling (`claim_only` / `evidence_weight` / `claim_risk`) is live and
  refuses to launder a claimed score.
- The verdict-wording gap (F2) is real and, at the *presentation* layer, closable with
  the same grading vocabulary already used for outcomes — done here.

**Does not establish:**
- That thread-membership inference (F1) is wrong in every case — only that it is
  *unchosen* and unsurfaced, which is the honesty gap.
- Anything about the reserved verification-weighted *risk math* — F2's code change is
  presentation-only and deliberately does not touch the actuator.
- A reproduction of the mirror bug (F5) — the session was too healthy to trip it.

Honest one-line framing: *the two findings the fleet has been hammering (token
confusion, self-report laundering) both show live remediation from a cold consumer chat;
the residual verdict-confidence gap is now honest in its wording, though the
verification-weighted risk math it points at remains reserved v2 work.*

---

## Relation to neighboring work

- [`self-report-verdict-dependence-2026-06-28.md`](self-report-verdict-dependence-2026-06-28.md)
  — the pre-warmup regime F2 sits in; this report's `explain_verdict` grading makes that
  regime's provisionality visible in the verdict wording.
- [`docs/proposals/verification-weighted-verdict-v0.md`](../proposals/verification-weighted-verdict-v0.md)
  and [`continuous-verdict-blending-v0.md`](../proposals/continuous-verdict-blending-v0.md)
  — the reserved v2 axis (verification-weighted *risk*, one-sided blend). F2's change is
  the presentation-layer complement, not a substitute; it must not be read as closing
  those.
- [`docs/proposals/redis-retirement-v0.md`](../proposals/redis-retirement-v0.md) — the
  constraint on where a short opaque continuity handle (F3) could resolve.
