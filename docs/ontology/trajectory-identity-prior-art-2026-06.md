# Prior-Art & Novelty Audit: Trajectory Identity, EISV Proprioception, Substrate-Earned Identity

**Status:** Research note (external-literature audit). Companion to
`trajectory-identity-philosophy-of-mind.md` (the essay this grounds),
`identity.md` (taxonomy + axioms), and `paper-positioning.md` (v7 framing).
**Date:** 2026-06-30.
**Method:** multi-source web research pass — 5 search angles, 27 sources
fetched, 61 candidate claims extracted, 25 adversarially verified (2-of-3
refute-to-kill); 24 confirmed, 1 refuted, synthesized to 10 findings. This
note records the surviving findings with citations.
**Scope caveat:** this situates the three constructs against *external*
academic/industry literature. It does not re-audit the in-repo design docs.
Several primary URLs returned HTTP 403 to direct fetch; those quotes were
corroborated via search-engine retrieval of source text, not byte-verified
(flagged inline).

---

## TL;DR

All three constructs are **partial rediscoveries**: each re-implements an
established academic framework in a novel *engineering* form rather than
introducing wholly new theory. That is not a criticism — it is the
positioning the papers should adopt. Ranked by how much genuinely novel
conceptual content survives the audit:

1. **Substrate-earned identity** — *most novel.* Its hardware/non-copyable
   half is mature security prior art, but its "continuity earned through
   sustained behavioral consistency *integrated* over time" half is only
   loosely prior-arted (narrative niche-construction) and is the strongest
   original contribution.
2. **Trajectory identity** — *partial rediscovery, novel implementation.* The
   *idea* (formal AI self-identity as continuity over a space) is already
   peer-reviewed prior art; the DTW + Bhattacharyya + genesis-signature
   *machinery* is a distinct realization.
3. **EISV proprioception** — *near-direct rediscovery.* The interoceptive-
   inference / active-inference literature already formalizes exactly what
   EISV names but has not implemented; EISV's novelty window is narrowing as
   that field converges (2024–2026).

**Three actionable takeaways:**

- The paper cited in `src/trajectory_identity.py:9` ("Trajectory Identity: A
  Mathematical Framework for Enactive AI Self-Hood") is **the project's own
  internal/unpublished work** (confirmed by the maintainer), not external prior
  art — a verbatim title search surfaces no public publication, as expected.
  The citation is legitimate; the only hygiene action is to mark it explicitly
  as an internal/unpublished work so a reader does not mistake it for a
  findable external reference, and — if it is ever published — to position it
  against the external prior art below (Lee's metric-space formalism; the
  enactivism lineage).
- **EISV should cite the active-inference / interoceptive-AI literature
  (Seth; Friston et al.; the Interoceptive Machine Framework) and position
  itself as an engineering instance, not a new theory.**
- The **Blockhead / integration-vs-replay** problem for AI behavior-as-identity
  is a *genuinely open* research gap — no surfaced source addresses it
  directly. This is an unclaimed contribution opening, not a solved problem.

---

## Construct 1 — Trajectory identity

**Verdict: partial rediscovery of "formal AI self-identity via continuity,"
with a novel DTW/attractor implementation.**

- **Closest prior art (concept).** Lee, *"Emergence of Self-Identity in AI: A
  Mathematical Framework and Empirical Study with Generative LLMs"* — arXiv
  [2411.18530](https://arxiv.org/abs/2411.18530), published in *Axioms* 2025,
  [doi:10.3390/axioms14010044](https://doi.org/10.3390/axioms14010044).
  Grounds AI self-identity in two conditions: a **connected continuum of
  memories in a metric space** plus a **continuous self-recognition mapping**
  across that continuum — explicitly positioned against "approaches that rely
  on heuristic implementations or philosophical abstractions." This is a
  topological/continuity formalism. *(Venue note: Axioms (MDPI) is a genuine
  peer-reviewed but modest-tier rapid-review journal.)*
- **Why UNITARES is still distinct (implementation).** UNITARES scores identity
  via Dynamic Time Warping over per-dimension EISV series
  (`_dtw_distance` / `_dtw_similarity`, `src/trajectory_identity.py:181-235`)
  plus Bhattacharyya distance over Gaussian attractors
  (`bhattacharyya_similarity`, `:40`) against a stored genesis signature. That
  is a behavioral-trajectory realization, *not* Lee's metric-space/topological
  one. The concept is prior-arted; the machinery is novel.
- **Enactive prior art (anticipates the grading).** Di Paolo (2005),
  *"Autopoiesis, Adaptivity, Teleology, Agency,"* *Phenomenology and the
  Cognitive Sciences* 4:429-452
  ([Springer](https://link.springer.com/article/10.1007/s10539-005-9134-y);
  [PDF](https://yannickprie.net/archives/ENACTION-SCHOOLS/docs/documents2006/autopoiesis_teleology_2005.pdf)):
  bare autopoiesis/self-maintenance is **insufficient** to constitute a
  sense-making self; one must add **adaptivity** — "a many-layered property
  that allows organisms to regulate themselves with respect to their
  conditions of viability," grading states *within* the viable region rather
  than registering only the binary alive/dead boundary. This maps directly
  onto UNITARES's graded identity ladder (unknown/emerging/established/verified)
  and onto EISV's viability bounds. Implication for the essay: a
  self-maintaining trajectory *shape alone* is, by the enactive critique, not
  yet a sense-making self.
- **Provenance of the cited paper.** "Trajectory Identity: A Mathematical
  Framework for Enactive AI Self-Hood" is **the project's own
  internal/unpublished paper** (maintainer-confirmed); a verbatim title search
  surfaces no public publication, as expected for an internal work. It is a
  legitimate internal citation, not external prior art — the external
  prior art for the *concept* remains Lee (2411.18530) and the enactivism
  lineage. If the internal paper is published, it should be positioned against
  those.

## Construct 2 — EISV proprioception

**Verdict: near-direct rediscovery of interoceptive inference under the Free
Energy Principle.** The literature already formalizes "sense your own internal
state, stay within viable bounds" as free-energy / prediction-error
minimization — the exact target the EISV contract names but has not yet
implemented.

- **Seth (2013), *"Interoceptive inference, emotion, and the embodied self,"*
  *Trends in Cognitive Sciences* 17(11):565-573**
  ([ScienceDirect](https://www.sciencedirect.com/science/article/pii/S1364661313002118);
  [PMID 24126130](https://pubmed.ncbi.nlm.nih.gov/24126130)): selfhood arises
  from "actively-inferred generative (predictive) models of the causes of
  interoceptive afferents" — i.e., the self is *constructed via inference, not
  stored.* The canonical predictive-processing account EISV reaches for.
- **Lee, Oh, An, Yoon, *Friston*, Hong, Woo, *"Life-inspired Interoceptive
  AI"* — arXiv [2309.05999](https://arxiv.org/abs/2309.05999)** (co-authored by
  the FEP originator): "Interoception is a process of monitoring one's internal
  environment to keep it within certain bounds"; prescribes factorizing
  internal-environment state variables from external ones — a self/world
  **Markov-blanket** boundary. Prior art for treating EISV as a distinct
  interoceptive channel separate from task observations.
- **Tschantz, Seth, Pezzulo (2022), *Biological Psychology***
  ([S0301051122000084](https://www.sciencedirect.com/science/article/pii/S0301051122000084)):
  "The goal of interoceptive control is to minimize a discrepancy between
  expected and actual interoceptive sensations (i.e., a prediction error or
  free energy)… homeostatic, allostatic and goal-directed." EISV's set-points
  and self-relative deviation are direct counterparts.
- **Interoceptive Machine Framework (2026), *Physics of Life Reviews***
  ([S1571064526000461](https://www.sciencedirect.com/science/article/pii/S1571064526000461)):
  organizes interoceptive AI into homeostatic / allostatic / enactive
  principles with concrete viability variables (energy, actuator strain,
  uncertainty/prediction-error statistics, latent-state stability) — the
  closest direct architectural prior art for EISV.
- **Active inference as the formal account of homeostasis/allostasis**
  ([PMC10839114](https://pmc.ncbi.nlm.nih.gov/articles/PMC10839114/), Frontiers
  in Neural Circuits 2024): interoceptive prediction-error minimization against
  set-points, as a multi-level generative model — bears on EISV's single-vector
  "thermometer" framing.

*Citation-hygiene note from the audit:* the Seth-2013 supporting quote was
corroborated via a faithful secondary commentary (Sel 2014, Frontiers in
Psychology) rather than Seth's own page; the Biological-Psychology quote was
mis-filed under a 2026 *Physics of Life Reviews* URL in the source pool but
traces to S0301051122000084. Cite the primaries above, not the aggregators.

## Construct 3 — Substrate-earned identity

**Verdict: splits cleanly. Hardware half = mature prior art; behavioral-integral
half = the most novel contribution of the three.**

- **Hardware / non-copyable half (well-covered prior art).**
  - TPM-based remote attestation as a hardware root of trust whose
    keys/measurements cannot be exfiltrated: *"TPM-Based Continuous Remote
    Attestation for 5G VNFs"* — arXiv
    [2510.03219](https://arxiv.org/html/2510.03219v1) (TPM 2.0 + Linux IMA,
    "hardware-based runtime validation").
  - Device-bound anonymous credentials with **cryptographically-enforced
    non-transferability:** Friedrichs, Lehmann, Lysyanskaya (Eurocrypt 2026),
    [IACR 2025/1995](https://eprint.iacr.org/2025/1995) — a credential tied to
    a secure-element-protected non-exportable key such that any presentation
    "requires a fresh contribution of the SE," formally modeling "unforgeability
    and non-transferability." This *cryptographically* prevents the
    cross-device copying that the substrate-earned construct argues is
    impossible for a carried string.
    ([Springer chapter](https://link.springer.com/chapter/10.1007/978-3-032-25317-0_12).)
  - **Continuous-verification principle:** Zero-Trust's "never trust, always
    verify" (3GPP TS 33.501 critique in arXiv 2510.03219) already states that
    one-shot authentication is insufficient and trust must be continuously
    re-verified — the credential-vs-sustained-trust distinction at the
    general-principle level.
- **Behavioral-integral half (genuinely less prior-arted — the novel part).**
  Heersmink (2020), *"Narrative niche construction: Memory ecologies and
  distributed narrative identities,"* *Biology & Philosophy* 35(5):48
  ([philarchive](https://philarchive.org/archive/HEENNC)): narrative identity
  is "distributed across embodied brains and an ecology of environmental
  resources" and is maintained by **active niche-construction** — "creating,
  editing, and using resources in our memory ecology," *not* passive storage.
  This is the closest prior art to the construct's "continuity earned through
  sustained behavioral consistency" claim, but it is philosophy, not a
  mechanism — the engineering of an *integrated* behavioral continuity remains
  largely open.

---

## The two open problems

### (a) Integrate-vs-replay — partially answered, with a real gap

The claim that "memory earns identity only when later instances *integrate* it
into behavior" (axiom #12) is supported by the narrative/distributed-cognition
tradition: identity is sustained by *actively using* memory resources
(Heersmink, above), not by their mere existence. That grounds the
integration-not-storage intuition.

**But the Block "Blockhead" / lookup-table objection as applied to AI
behavior-as-identity — distinguishing genuine behavioral *integration* from
sufficiently rich *replay* — was not addressed by any surfaced source.** This
is exactly the essay's §5(c) point (the deterministic-cron-scores-as-perfect-
continuity case, limitation v3.2-F). It is a **genuinely open research gap**,
not a solved problem — and therefore an unclaimed contribution opening.

### (b) Integral-self vs present-tense accountability — the literature says *bifurcate*

This is the most useful finding for the architecture. The narrative-identity
tradition affirms identity as a temporally-extended *construction* (not a
present-tense fact), which *sharpens* the essay's §5(a) tension rather than
dissolving it. The security literature supplies the present-tense gate
separately — via a hardware-bound credential checked per-presentation
(attestation / device-binding above).

No surfaced source unifies the two into a single construct. The literature thus
**implies bifurcation:** integral-over-time identity for *continuity /
accountability*, plus a separate hardware-anchored credential for *present-tense
authorization.* This **vindicates UNITARES's existing two-layer design** —
trajectory identity and substrate/bearer binding as *separate* mechanisms (the
essay's §4 "division of labor"). The present-tense hole in §5(a) is not a hole
to patch inside trajectory identity; it is correctly the job of a different
layer.

---

## Failure modes the prior art predicts

- **Behavioral signatures are forgeable.** Treadmill-Assisted Gait Spoofing
  (ACM IMWUT, [doi:10.1145/3442151](https://doi.org/10.1145/3442151); arXiv
  [2012.09950](https://arxiv.org/pdf/2012.09950)) raised average False-Accept
  Rate from 4% to 26% (~6.5×) "despite the use of a variety of sensors, feature
  sets… and six different classification algorithms." Construct 1 is explicitly
  scoped to *honest over-claims*, not adversaries (R1 non-goal "Not
  authentication"), so this is a **boundary, not a break** — but it confirms
  why that scoping is load-bearing: a behavioral trajectory must never be
  treated as an authentication credential.
- **Concept drift breaks fixed baselines (template aging).** Maciejewski et al.
  (*Engineering Applications of AI*, 2020,
  [S0952197620303729](https://www.sciencedirect.com/science/article/abs/pii/S0952197620303729)):
  legitimate users' behavioral features change over time; drift-adaptive models
  deliver higher accuracy and lower FAR/FRR than static baselines. **Bears
  directly on the immutable-genesis-at-tier-2+ rule** (`store_genesis_signature`):
  the prior art predicts a fixed genesis will age out of the legitimate agent's
  own evolving behavior. The tier-1 reseed path partially mitigates this, but
  the immutability lock at tier 2+ is precisely the static-template risk the
  literature flags. *(This finding carried a 2-1 verifier vote on the strength
  of the AI-agent extrapolation; the underlying biometrics principle is
  unanimous.)*

---

## Caveats & residual uncertainty

- Many primary URLs (ScienceDirect, PMC, Springer, IACR, arXiv HTML) returned
  HTTP 403; quotes verified via search-engine retrieval, not byte-for-byte.
- *Axioms* (MDPI), publishing Lee's metric-space paper, is a genuine
  peer-reviewed but modest-tier venue.
- The FEP/active-inference and device-bound-credential literatures are
  fast-moving (2024–2026); the interoceptive-AI framing in particular is
  converging rapidly, so **EISV's window to claim conceptual novelty is
  narrowing.**
- One enactivism+DST source initially proposed as direct Construct-1 prior art
  (Frontiers in Psychology 2014.00452) was **refuted** (1-2 vote) and excluded.

## Open questions carried forward

1. The internal paper "Trajectory Identity: A Mathematical Framework for
   Enactive AI Self-Hood" (project-authored, unpublished) — does it engage Lee
   (2411.18530) or the enactivism literature, and is it intended for
   publication? If so, the prior art here is its related-work scaffolding.
2. Does *any* existing work unify an integral-over-time behavioral identity
   with a present-tense authorization gate in one construct, or does the field
   consistently bifurcate them (as the sources here imply)?
3. Is there formal/empirical work on the Blockhead/lookup-table objection for
   AI behavior-as-identity — genuine integration vs. rich replay? (None
   surfaced; named open gap.)
4. How robust is a DTW + Bhattacharyya genesis-signature scheme *specifically*
   to both imitation/forgery and legitimate concept drift — has anyone
   benchmarked dynamical-invariant trajectory matching for software-agent
   self-models under adversarial and drift conditions?
