# Paper Positioning: Identity Ontology and Paper v7

**Answers:** Q3 from `docs/ontology/plan.md` — is the identity ontology paper v7's animating thesis, or an implementation detail that can stay in `docs/ontology/`?
**Read:** paper v6.8.1 tag `paper-v6.8.1` (latest master); specifically §4 "Class Structure in the Identity Layer" and §6.7 "Runtime labels and audit vocabulary" remark.
**Compared against:** `docs/ontology/identity.md` v2.

---

## TL;DR

**Recommendation: v7 animating thesis.** The ontology is upstream of v6's class-conditional grounding — it explains *why* class-conditional grounding is principled and completes a frame v6 currently achieves only implicitly. Reframing v7 around the ontology also carries the paper during the corpus-maturity blocker (~Q3 2026) by offering a non-empirical contribution independent of audit-log joins.

## What v6.8.1 already does

v6 frames identity as **class structure for calibration purposes.**

Specifically, §4 (Class Structure in the Identity Layer) does three things:

1. **Tags as governance partition.** Every agent carries tags from `{embodied, autonomous, persistent, ephemeral, pioneer}`. Tags partition the population along governance-relevant axes: embodiment (output channel), autonomy (self-directed vs prompted), persistence (trajectory accumulates or resets).
2. **Class-conditional calibration.** Distinct scale constants and healthy baseline points per class, with fleet-wide constants as fallback. §5 (Class Calibration) formalizes this as the Phase-2 grounding contribution.
3. **Heterogeneity as differentiator.** §4.2 — "the same dynamics describe a heterogeneous population without collapsing the classes."

And §6.7 (Runtime labels remark) adopts a compatible pattern: different surfaces (runtime / audit / binary-verdict) carry different vocabularies, all reducing to the same formal contract.

**This is the seed of the ontology.** v6 is already doing the work the ontology formalizes — it just doesn't have a frame for calling it that.

## What the ontology adds

Five things v6 currently does not have and would benefit from:

1. **Three stances** (performative / descriptive / inventive). v6 is descriptive-stance — it reports class structure. It does not distinguish earned from performative continuity, and it does not open a research agenda for turning performative into earned. The stances axis makes this distinction available.
2. **Five-layer continuity taxonomy** (process-instance / substrate / role / memory / behavioral). v6's tags conflate several of these. "persistent" vs "ephemeral" is a crude mix of substrate + behavioral + memory continuities. The taxonomy decomposes this and makes it audit-able.
3. **Synthetic Life Axioms as gate** (KG `2026-04-02T05:13:26.577769`). The axioms already constrain the system but are unpublished. Folding them into the paper makes the theoretical contribution substantially stronger and gives UNITARES a normative spine that is absent from the current v6 framing.
4. **Substrate-earned identity as a formal pattern** (R4). Lumen's hardcoded UUID becomes principled — the substrate makes a commitment lesser substrates cannot. v6 already carries this implicitly in the `embodied` tag + per-class calibration scales, but does not explain it.
5. **Research agenda for genuine continuity tech** (R1, R2, R5). The inventive stance opens contributions v6 does not make and that competing governance frameworks do not offer — behavioral-continuity verification, honest memory integration, memory-deepening tooling.

## Convergence points

| v6.8.1 element | Ontology element | Relationship |
|---|---|---|
| Tags {embodied, autonomous, persistent, ephemeral, pioneer} | Five-layer taxonomy | v6 tags are informal projections onto the formal layers. `embodied` ≈ substrate layer; `persistent`/`ephemeral` ≈ substrate+memory combined. |
| Class-conditional calibration (§5) | Substrate-earned identity (R4); re-interpretation of trust tier (S6) | Same pattern. Ontology explains *why* class-conditioning is principled: because different substrates earn different degrees of continuity. |
| Heterogeneity as differentiator (§4.2) | Three stances + layered taxonomy | Ontology grounds the differentiator claim — heterogeneity matters *because* different agents occupy different regions of the continuity taxonomy, not just because of dynamics. |
| §6.7 runtime-vs-audit vocabulary remark | Nomenclature: role / governance-identity / lineage distinctions | Both adopt the pattern "different surfaces, different vocabularies, same underlying contract." Ontology generalizes this pattern. |
| `ephemeral` class bypass of PID loop (§5.4) | Subagent ephemerality rule (Q2) | v6 handles ephemeral classes by bypass; ontology asks whether this is principled or pragmatic. Ontology's answer depends on R1. |

## Where v6 could resist the ontology

- **v6 treats UUID as unproblematic** — the accumulator of trajectory, identity for all purposes. The ontology problematizes this. A v7 reframing would have to introduce the performative-vs-earned distinction without invalidating v5/v6's stability results (which hold at the dynamics layer, untouched by ontology choice).
- **Moving-goalposts risk.** If reviewers read v5/v6 as "the core contribution is dynamics + class-conditional grounding," pivoting v7 to ontology could be read as scope expansion to hide the v7 empirical blocker. Mitigation: frame the ontology as **completing** v6 (making its implicit frame explicit) rather than replacing it. The stability results and class-conditional calibration remain; the ontology gives them a principled grounding they did not previously have.

## Recommendation and reasoning

**v7 animating thesis.** Rationale:

1. **Upstream, not adjacent.** The ontology is philosophically prior to v6's technical framework. Papering it as implementation detail would bury the thing that actually explains why v6 works.
2. **Completes v6's implicit frame.** v6's "heterogeneity as differentiator" claim is stronger when grounded in a taxonomy of what heterogeneity means for identity. The ontology provides that taxonomy.
3. **Publishes the Synthetic Life Axioms.** The axioms are canonical in the system (KG entry, tagged `foundational`, `critical` severity) but have never been formally published. v7 is the right vehicle. The axioms provide the normative frame the paper-series currently lacks.
4. **Carries the corpus-maturity gap.** v7 empirical work blocks on the post-grounding audit corpus reaching maturity (~Q3 2026 per `project_paper-v7-corpus-maturity.md`). Reframing v7 around ontology + axioms gives the paper a substantial non-empirical contribution that can be written and submitted independently of corpus maturity, with empirical extensions landing in v7.1 or v8.
5. **Differentiates from competing frameworks.** Other governance frameworks do not distinguish performative from earned continuity. An animating ontological contribution is harder to replicate than a technical one.

## Risks and mitigations

- **Risk: reviewers perceive scope drift.** Mitigation: structure v7 as "v6 + animating frame," not "v7 ≠ v6." Keep §4 Class Structure and §5 Class Calibration; add a new §3 (or revise §2) that introduces the three stances + taxonomy + axioms, and revise §4 as the instantiation of that frame.
- **Risk: ontology is less mature than v6's technical apparatus and could weaken the paper.** Mitigation: the ontology as drafted is descriptive-stance — it reports what is. The inventive-stance agenda (R1-R5) is explicitly future work. v7 positions the descriptive taxonomy + axioms as contribution; R1-R5 are proposals, not claims.
- **Risk: philosophical framing attracts philosophy reviewers who want deeper engagement with person-identity literature (Parfit, Locke, etc.).** Mitigation: cite briefly; defer deep engagement to a companion philosophy paper if it emerges. The core contribution is the taxonomy + axioms + grounded applications, not a philosophical theory of identity.

## Concrete next step

If recommendation accepted: v7 outline draft. Proposed structure:

1. Introduction (unchanged in spirit; new animating thesis)
2. **Three Stances and the Synthetic Life Axioms** (new — pulls from axioms KG + ontology doc)
3. **Layered Continuity Taxonomy** (new — five layers + worked examples with the production agent classes)
4. Class Structure in the Identity Layer (revised — positioned as instantiation of the taxonomy, not as standalone contribution)
5. EISV Dynamics (unchanged from v6)
6. Class-Conditional Grounding (revised — explains *why* via the ontology, then presents scale-constant machinery as before)
7. CIRS + Drift Vector (unchanged)
8. Evaluation (v7-appropriate scope; smaller than v6 pending corpus maturity)
9. **Research Agenda** (new — the R1-R5 items from plan.md presented as future-work propositions)
10. Related Work (add ontological/philosophical citations as light brush)
11. Conclusion

Outline can land in the paper repo (`unitares-paper-v6`) as a draft of `unitares-v7-outline.tex` (or a branch of v6 renamed v7) without blocking on this recommendation's acceptance — it's an artifact you can read, not a commitment to submit.

## If recommendation rejected

If the lean is "ontology stays in `docs/ontology/` as implementation detail, v7 remains an empirical-corpus-maturity paper with a class-conditional grounding contribution": no paper work needed. The ontology docs stand on their own; code-level work (tracks B and C) proceeds independently. Paper v6.9 glossary may still mirror ontology nomenclature for cross-reference, but the paper's animating frame does not change.

---

## Structural retentions: v7 main-text sections (2026-04-23 correction)

A 2026-04-23 discussion raised the FEP-grounding departure (see `v7-fhat-spec.md`). In the same thread, the same process-instance sketched a broader list of v6 main-text sections to demote for v7 — specifically §5 (Contraction / Stability), §6 (CIRS v2 / PID governor), and §8 (Multi-Agent Network / Synchronization). **Those demotion suggestions are retracted.** Reason: v7's animating thesis is heterogeneity-as-differentiator. That thesis requires the within-class dynamical infrastructure — stability (§5), class-conditional governance decision (§6), and within-class synchronization (§8) — to be mathematically backed, not hand-waved. Demoting those sections would turn the heterogeneity claim into rhetoric about different classes having different norms, with no dynamical-system substrate showing each class is internally coherent and each class's state produces a principled verdict.

The only v6 main-text section that v7 retires with confidence is the FEP-grounding of $E$ as "negative variational free energy" (v6 §3.1, §3.2 $E$-paragraph), because the current computation does not honor the claim. That departure is scoped to the $E$ coordinate's semantic source; it does not imply broader section deletions.

**For future v7 structure work:** §5, §6, §8 stay in main text. §7 (Stochastic Extensions) is 1.5 pages and is left alone unless page budget becomes tight. §10 (Related Work) expands substantially (four-anchor spine from 2026-04-23 analysis). New §2 (Three Stances + Synthetic Life Axioms), §3 (Layered Continuity Taxonomy), and §9 (Research Agenda) are additive; they do not replace existing sections. The v6→v7 delta is net-additive, not net-deletive, apart from the $E$-as-$-F$ claim — see the 2026-04-23 update below, which widens the FEP-grounding retirement.

## 2026-04-23 update: v7 accepts path (b); FEP grounding demoted, not disproven

The v7 F-hat spike (spec at `docs/ontology/v7-fhat-spec.md`) completed Session 1b on master `fdc2d180`. The sanity gate SC2 (denoising-collapse check, spec §2.6) tripped at Pearson $r = 0.9949$ on 952 validation rows, indicating that under the v5-amended observation geometry (C1–C4 direct EISV measurements plus sparse C5, with C6 dropped because those channels had zero history in the reference window), the fitted $\hat{F}_t$ reduces to $\|o^{\text{chk}}_t - \mu_{t|t-1}\|_2$ up to monotone transform. Running Session 2's horse race against B2 (raw-EISV logistic) would have been pseudo-validation — comparing $\hat{F}$ to a near-equivalent scalar under the same latent. Operator selected **R1 (accept path (b) early)** on 2026-04-23; the eval slice was not touched.

**Consequences for v7 structure:**

- The retirement list is no longer "only $E$-as-$-F$." **$V$'s FEP grounding also retires** (v6 §3.2 $V$-paragraph's "accumulated free-energy residual" interpretation). $V$ is reframed phenomenologically as a damped accumulator of the $E{-}I$ gap, per v7 F-hat spec §5.1(b). Friston-derivation language in that paragraph is deleted rather than reparented.
- FEP as a whole moves from **load-bearing grounding** to **adjacent / inspirational framing** in v7's narrative. The paper still cites active inference as a coherent prior-art neighbor; it does not claim that UNITARES's $E$, $V$, or coherence are variational free-energy quantities in any formal sense.
- v7's §3 coordinate-table rewrite is the **path (b) variant** (spec §5.1(b)), not the (d) variant. The $E$ and $V$ rows lose FEP labels; both gain phenomenological labels; the $\hat{F}$-debt reparenting prepared for path (d) does not land.
- No consequence for v7's animating thesis. The **heterogeneity-as-differentiator** frame was chosen independent of FEP grounding (see "Structural retentions" above). §5 (Stability), §6 (CIRS v2), §8 (Synchronization) remain mathematically backed without FEP scaffolding. The Synthetic Life Axioms, Layered Continuity Taxonomy, and Research Agenda are additive and unaffected.

**Blocked, not disproven.** The spike did not demonstrate that a variational-free-energy grounding of governance is *in principle* unavailable for UNITARES. It demonstrated that the **minimal generative model is too thin under the current observation-channel geometry** — when the only channels are direct measurements of the latent, $\hat{F}$ has no asymmetric-information term to distinguish it from residual magnitude. A later revisit with at least one asymmetric channel (matured C6 event stream ≥ 30 days of history; historically-pullable per-agent calibration state; `primitive_feedback` / `watcher_finding` shipped as first-class audit channels) could re-open the question. This is v7.1 / v8 instrumentation work, not a v7 blocker.

Paper-language posture: active inference appears in the Related Work section as a prior-art neighbor; it does not appear as a grounding claim in §3. The v6 "$E = \sigma(-F/E_{\text{scale}})$" equation is removed; $E$ is introduced phenomenologically as productive-capacity, consistent with the resource-rate heuristic production has always used.

---

## 2026-06-30 update: interoceptive-inference prior art for EISV (Related Work spine)

A prior-art audit (`docs/ontology/trajectory-identity-prior-art-2026-06.md`)
sharpened the active-inference neighbor for the specific case of **EISV framed
as proprioception/interoception**. The Related-Work §10 spine should name the
*interoceptive*-inference branch by name, not just generic active inference, and
should position EISV as an **engineering instance of an existing framework, not
a new theory** — the audit's verdict was "near-direct rediscovery," and the
field is converging fast (2024–2026), so the honest framing also protects the
paper from a novelty overclaim.

Anchor citations (all corroborate "sense your own internal state, stay within
viable bounds" as the prior art EISV's contract describes):

- **Seth (2013), *"Interoceptive inference, emotion, and the embodied self,"*
  *Trends in Cognitive Sciences* 17(11):565-573** — the canonical
  predictive-processing account: selfhood from actively-inferred generative
  models of interoceptive causes (self *constructed*, not stored).
- **Lee, …, Friston, …, *"Life-inspired Interoceptive AI,"* arXiv 2309.05999**
  — Friston-co-authored; "monitoring one's internal environment to keep it
  within certain bounds," with a self/world **Markov-blanket** factorization
  (prior art for treating EISV as a distinct interoceptive channel).
- **Tschantz, Seth, Pezzulo (2022), *Biological Psychology*** — interoceptive
  control = minimizing prediction error (free energy) against set-points;
  homeostatic / **allostatic** / goal-directed. The allostatic branch is also
  the named mechanism in `docs/proposals/genesis-baseline-aging-v0.md`.
- **Interoceptive Machine Framework (2026), *Physics of Life Reviews*** — the
  closest architectural prior art: homeostatic/allostatic/enactive principles
  with concrete viability variables (energy, actuator strain, prediction-error
  statistics, latent-state stability).

**Posture (consistent with the 2026-04-23 demotion above):** these are
Related-Work *neighbors*, not grounding. The paper cites interoceptive inference
as the framework EISV instantiates in engineering form; it does **not** claim
EISV's coordinates are variational free-energy quantities (that claim retired
with the F-hat spike). The contribution is the engineering realization +
heterogeneity frame, not a new theory of interoception. This also pre-empts the
§66 risk (philosophy/cog-sci reviewers wanting deeper literature engagement) by
citing the precise neighbor rather than gesturing at "active inference."

---

**Revision history:**
- 2026-04-21 — original recommendation.
- 2026-04-23 — structural-retention correction.
- 2026-04-23 — Session 1b fit, halt, R1 resolution.
- 2026-06-30 — interoceptive-inference prior-art spine for EISV (from the prior-art audit).
