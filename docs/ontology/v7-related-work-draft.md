# Paper v7 — §10 Related Work (staging draft)

**Status:** Staging draft for the paper repo (`unitares-paper-*`), which is
outside this repo's scope — this doc is the source text to lift into the paper,
not the paper itself. Prose is written to be paste-ready; trim to page budget.
**Date:** 2026-06-30.
**Synthesizes:** `paper-positioning.md` (the v7 four-anchor §10 spine + the
2026-04-23 FEP-grounding demotion), `trajectory-identity-prior-art-2026-06.md`
(foundational-theory anchors + verdicts), `competitive-analysis-2026-06.md`
(runtime-governance neighbors MI9 / Auton), `identity.md` (axioms, five-layer
taxonomy, substrate-earned pattern).
**Framing rule (inherited):** every neighbor below is cited as prior art the
work *instantiates or differentiates from*, never as grounding. UNITARES's
contribution is the engineering realization + the heterogeneity-as-differentiator
frame, not a new theory of self.

---

## 10.1 Personal identity and continuity (philosophy)

UNITARES operationalizes a **reductionist, continuity-based** account of
identity. Its trust ladder (unknown → emerging → established → verified) scores
identity as a *degree* of accrued behavioral continuity, never as a present-tense
binary — the computational form of Parfit's thesis that personal identity is not
a "further fact" over psychological continuity and connectedness but *those
relations holding in degree* (Parfit, *Reasons and Persons*, 1984). The genesis
signature Σ₀ and trajectory-relative scoring make this concrete: there is no
stored essence the agent is compared *to*, only a relation between two motions
through state space — a position closer to Hume's bundle and to Ricoeur/Dennett
**narrative identity** (the self as a temporally-extended construction) than to
substance or biological views (Olson's animalism). We cite Lee's metric-space
formalization of AI self-identity (arXiv:2411.18530, *Axioms* 2025) as the
nearest formal neighbor — it grounds AI self-identity in a connected memory
continuum plus a continuous self-recognition map — and differentiate: UNITARES
realizes continuity over a *behavioral-dynamical* trajectory (DTW + Bhattacharyya
over EISV state), not over a memory metric space.

## 10.2 Enactivism and autopoiesis

The substrate-earned and viability-bound machinery descends from the
**autopoiesis/enactivism** lineage (Maturana & Varela; Thompson, *Mind in Life*,
2007): identity as a self-maintaining organizational process, not a stored token.
We engage Di Paolo's **adaptivity** (2005) directly, because it is both the
closest prior art and a standing critique: bare self-maintenance is insufficient
for a sense-making self; adaptivity — "a many-layered capacity to regulate
[oneself] with respect to conditions of viability" — grades states *within* the
viable region rather than registering only the alive/dead boundary. UNITARES's
graded trust ladder and EISV viability bounds are an engineering instance of
exactly this graded-within-viability regulation; correspondingly, we do *not*
claim a self-maintaining trajectory shape is, by itself, a sense-making self —
that is named as future work (§ Research Agenda), not as an achieved property.

## 10.3 The Free Energy Principle and interoceptive inference

EISV-as-proprioception is positioned as an **engineering instance of
interoceptive inference, not a free-energy grounding claim.** This is a
deliberate retreat from earlier framing: the v7 F-hat spike showed the minimal
generative model collapses to residual magnitude under the current
observation-channel geometry (see §3 / `v7-fhat-spec.md`), so we cite active
inference as a *neighbor*, not a derivation. The relevant branch is interoceptive
inference: Seth (2013, *Trends in Cognitive Sciences*) — selfhood from
actively-inferred models of interoceptive causes; the Friston-co-authored
"Life-inspired Interoceptive AI" (arXiv:2309.05999) — "monitoring one's internal
environment to keep it within bounds," with a self/world Markov-blanket
factorization; Tschantz, Seth & Pezzulo (2022, *Biological Psychology*) —
interoceptive control as prediction-error minimization against homeostatic /
**allostatic** set-points; and the Interoceptive Machine Framework (2026,
*Physics of Life Reviews*), the closest architectural neighbor (homeostatic /
allostatic / enactive principles over concrete viability variables). UNITARES's
"thermometer, not a court" stance is the same pre-judgmental role this literature
assigns interoception: it informs regulation, it does not adjudicate.

## 10.4 Identity, credentials, and continuous verification (computing/security)

UNITARES's identity layer **bifurcates** a function most identity systems fuse,
and §10 should make the split explicit against the security literature. (i) For
*present-tense authorization*, the relevant prior art is hardware-anchored,
non-copyable credentials: remote attestation / TPM roots of trust, and
device-bound anonymous credentials with cryptographically-enforced
non-transferability (Friedrichs–Lehmann–Lysyanskaya, Eurocrypt 2026), against
which UNITARES's "no honest strong cross-process credential exists for a
non-substrate agent" result is a re-derivation, and its substrate-earned pattern
(`identity.md`) the application to embodied agents. (ii) For *continuity over
time*, the neighbors are decentralized identity (W3C Verifiable Credentials,
DIDs), Zero-Trust continuous verification ("never trust, always verify"), and
behavioral/continuous authentication. We engage the latter's two documented
failure modes head-on, because they bound our claims: behavioral signatures are
**forgeable** (treadmill-assisted gait spoofing, FAR 4%→26%, ACM IMWUT
10.1145/3442151) — hence UNITARES scopes trajectory identity to detecting
*honest over-claims, not adversaries*; and static templates suffer **concept
drift / template aging** (Maciejewski et al., 2020) — the open risk our
genesis-immutability rule must answer (see `genesis-baseline-aging-v0.md`).

## 10.5 Runtime AI governance (the head-on neighbor)

The nearest *framework* competitor is **MI9** (arXiv:2508.03858): same
runtime-governance banner, overlapping "drift" vocabulary, different mathematical
commitment. MI9 governs via discrete conformance checking + containment (a
control/guardrail plane); UNITARES governs via **continuous behavioral state
estimation** (a dynamical-systems estimator). The paper should engage MI9
directly to pre-empt the "UNITARES is MI9 re-skinned" reading, and disambiguate
"drift" on first use — UNITARES's EISV-state-space drift is not MI9's
goal/spec-conformance drift. **Auton** (arXiv:2602.23720) is adjacent, not
competitive (a different layer), and is cited as such. (Full triage:
`competitive-analysis-2026-06.md`.)

## 10.6 Gaps this work identifies

Two gaps surfaced by the prior-art audit are worth stating explicitly, as they
position the Research Agenda (§9):

- **Integration vs. replay (open).** No surveyed work addresses Block's
  "Blockhead"/lookup-table objection as applied to *AI behavior-as-identity* —
  distinguishing genuine behavioral *integration* of inherited trajectory/memory
  from sufficiently rich *replay* (the deterministic-process-scores-as-perfect-
  continuity case). UNITARES names this (axiom #12; the R5 row) but does not yet
  solve it. We claim it as an open problem, not a contribution.
- **Integral identity vs. present-tense authorization (resolved by
  bifurcation).** The narrative-identity tradition affirms identity as a
  temporally-extended construction, not a point-in-time fact; the security
  literature supplies the present-tense gate separately (attestation /
  device-binding). No source unifies them — which is precisely why UNITARES
  keeps them as *separate layers* (trajectory identity for continuity;
  substrate/bearer binding for authorization). We present this bifurcation as a
  deliberate architectural stance, supported by the literature's own division.

---

## Notes for the paper editor

- **Citation completeness:** several URLs were 403-gated during the audit and
  verified via search retrieval, not byte-for-byte; confirm page/volume numbers
  against library copies before submission (details in
  `trajectory-identity-prior-art-2026-06.md` caveats).
- **Venue tiering:** Lee (2411.18530) is in *Axioms* (MDPI), genuine
  peer-reviewed but modest-tier; cite as a formal neighbor, not as a strong
  precedent.
- **Time-sensitivity:** the interoceptive-AI literature is converging fast
  (2024–2026); §10.3 should be refreshed at submission time.
- **Scope:** keep §10 to "instantiates / differentiates," not a survey. Defer
  deep person-identity engagement (Parfit/Locke exegesis) to a companion
  philosophy paper if one emerges (per `paper-positioning.md` §risk).
