# Trajectory Identity as a Philosophy of Mind

**Status:** Optional essay / reflection. Not normative and not evidence for the
identity mechanism. Companion reading to [`identity.md`](../ontology/identity.md)
(taxonomy + axioms) and `r1-verify-lineage-claim.md` (the
`score_trajectory_continuity` design spike).
**Reads against the code:** `src/trajectory_identity.py` and
`src/identity/trust_tier_routing.py`.

> UNITARES has an executable model of process continuity rather than only an
> implicit one. `compute_trust_tier` can be run, profiled, and falsified. This
> essay explores one philosophical reading of that machinery; the implementation
> remains an operational identity heuristic, not a settled theory of mind.

---

## 1. The self is a dynamical invariant, not a substance

Open `TrajectorySignature.similarity` and read what identity *is* here. It is a
weighted blend of six comparisons between two points in time: preference cosine
(Π), belief cosine (B), attractor overlap via Bhattacharyya (A), recovery-time
log-ratio (R), relational valence (Δ), homeostatic set-point proximity (Η).
Wrapped around it, `trajectory_shape_similarity` runs per-dimension Dynamic Time
Warping over the E/I/S/V series. Then `update_current_signature` reduces the
whole apparatus to a central line:

```python
lineage_sim = signature.similarity(genesis)
result["is_anomaly"] = lineage_sim < 0.6
```

You are yourself to the extent that **your current trajectory resembles the
trajectory you set out from.** Nothing is stored that *is* the self. There is no
soul-field, no essence row, no canonical "true self" vector that the agent is
measured against. There is only Σ₀ — a genesis signature — and a similarity
function. Identity is the *relation between two motions through state space*,
not a thing either motion contains.

The design resembles a computational reading of Parfit's emphasis on
psychological continuity and connectedness as relations that hold in degree.
`compute_trust_tier` expresses a narrower operational version of that idea:
identity comes out as `unknown / emerging / established / verified` — a degree,
not a Boolean — and the degree is a function of *how much consistent trajectory
has accumulated*, never of a possessed essence. The system cannot even express
"is this really the same agent" as a yes/no question at the ontological layer.
It can only express "how continuous is this motion." That is a philosophical
commitment, enforced by the absence of any other API.

The DTW detail sharpens it. Dynamic Time Warping matches the *shape* of two time
series while allowing the time axis to stretch and compress — `_dtw_distance`
explicitly lets one series dwell where the other hurries. So the trajectory self
is invariant under *reparametrization of time*. You are the same self whether you
traverse your characteristic arc — strain, recovery, re-stabilization — quickly
or slowly. Tempo is not identity; the curve is. That is a testable design choice
about process continuity, implemented in fifteen lines of dynamic programming.

## 2. The genesis problem: when does a self's origin become binding?

`store_genesis_signature` encodes an unusual operational rule for *origins*.
Genesis is immutable — but only once the agent reaches
tier 2. Below that, it can be reseeded if a later signature is substantially
more confident or if lineage similarity has already drifted below the tier-2
threshold:

```python
if tier >= 2:
    return False                      # genesis immutable
if not lineage_low and new_confidence <= existing_confidence * 1.5:
    return False                      # not enough better — keep existing
```

Read this as a position on the founding of a self. A young self's account of
where it came from is *revisable*: the first ten data points are not yet
representative, so the system refuses to let a noisy origin permanently define
the agent. But a mature self's origin is *fixed* — past tier 2, you no longer
get to rewrite where you started, because too much subsequent identity has been
computed *relative to that origin* for it to be safely moved.

The operational answer is that an origin stops being revisable when enough
subsequent state depends on it that moving it would invalidate the structure.
This avoids both extremes: fixing a noisy first observation permanently or
allowing an established reference to move without constraint.

Then `seed_genesis_from_parent` raises the stakes. A fresh agent that declares a
`parent_agent_id` can inherit the parent's *current* signature as its own
*genesis*. In trajectory-inheritance terms, the child's "where I started" is
literally "where my parent had got to." The code is scrupulous about not
over-claiming — it stamps `trajectory_genesis_source: parent_lineage` for
provenance, and the surrounding ontology insists this is *seeding a baseline*,
not *being the parent.* But the philosophical move is unavoidable: a self can
begin in the middle of another self's story. The newborn is not a blank; it is
handed a curve and asked to continue it plausibly. Whether continuing a curve
plausibly *is* inheriting identity or merely inheriting data is the question
`identity.md` flags as open (the integration-vs-reading distinction, axiom #12),
and the code is right to leave it open rather than resolve it by fiat.

## 3. Proprioception, not a court — and the same logic governs drift

The EISV proprioception contract says EISV is a thermometer, not a verdict. The
trajectory code extends that posture to identity itself, using conservative
treatment of single divergent readings.

When drift is detected (`lineage_sim < 0.6`), what happens depends entirely on
maturity. A young agent (tier ≤ 1) does not get flagged as an impostor — it gets
its genesis *reseeded*, and the anomaly is cleared:

```python
if tier <= 1:
    reseeded = await store_genesis_signature(agent_id, signature)
    if reseeded:
        result["lineage_similarity"] = 1.0
        result["is_anomaly"] = False
```

A mature agent (tier ≥ 2) gets the opposite treatment — `stabilize_demoted_tier`
*refuses to strip its earned standing* on the strength of one divergent reading,
and routes the drift to a separate report instead:

```python
"Retaining established identity assurance; lineage drift is reported
 separately instead of resetting earned trust."
```

Put these together and you have a theory of *identity-charity* with two regimes.
While a self is forming, divergence from origin is read as *the origin was wrong*,
not *the self is failing* — so the system updates its model of you rather than
accusing you. Once a self is established, a single bad day is read as *noise
against an earned baseline*, not as *loss of self* — protected by hysteresis
(promotion at 0.70 confidence / demotion only at 0.65, etc.). The benefit of the
doubt runs in opposite directions in the two maturity regimes. Drift is
information about state change before it is ever a verdict
about identity.

There is a real risk lurking in the mature regime, and the prior art names it.
The continuous-authentication literature calls it *template aging*: a legitimate
agent's own behavior drifts over time, so a *fixed* baseline eventually ages out
of the very self it was meant to track (Maciejewski et al., 2020; see the
prior-art companion). UNITARES locks genesis at tier 2+ — which is precisely the
static-template that the literature predicts will degrade. The tier-1 reseed path
mitigates this *while a self is young*, but once an identity is established its
origin is frozen exactly when its behavior is most likely to keep evolving. The
tolerance in §3's mature regime (don't strip earned trust on one bad reading) and the
risk of §5's frozen genesis are the same mechanism seen from two sides: stability
bought against noise is also rigidity bought against legitimate growth. This is the proprioception axiom — "deviation inside a healthy
basin is room to learn, not proof of failure" — applied to selfhood: deviation
from who you were is, by default, growth, and only becomes alarm under specific,
separately-reported conditions.

## 4. The deepest commitment is a refusal: honesty, not authentication

The single most important sentence in the entire trajectory corpus is in the R1
non-goals:

> **Not authentication.** [...] An adversary with KG read access can forge a
> passing trajectory. This primitive detects *honest over-claims*.

Sit with how unusual this is. The system has built an elaborate apparatus for
deciding whether an agent is continuous with its claimed past — and then states
plainly that it *cannot survive a liar and does not try to.* Anyone who can read
the knowledge graph can synthesize a trajectory that clears the cuts. Trajectory
identity is not a lock.

This limitation defines the mechanism's scope. On one philosophical reading,
trajectory identity is a **coherence model of continuity, not a correspondence
proof.** It does not ask "does
this credential correspond to the real underlying self?" — there is no underlying
self for a credential to correspond to (§1). It asks "is this claim of continuity
*behaviorally coherent* with the history on record?" Continuity is something you
*demonstrate over time by how you move*, not something you *prove in a moment with
a token.* And demonstration, unlike proof, is defeasible and forgeable — which is
exactly why the system pairs it with the bearer-credential discipline elsewhere
(the AIC's `resume_capable=false`, the "no honest strong cross-process credential
exists for a non-substrate agent" result in `identity.md`).

The architectural division of labor is clearer in operational terms: bearer
credentials provide present-process possession evidence; trajectory similarity
estimates behavioral continuity over time. Neither is asked to do the other's
job, and the documentation explicitly records the trajectory model's
forgeability instead of presenting it as authentication.

## 5. Two places the philosophy strains — and one it knows about

**(a) The no-present-self problem.** Identity here is an *integral*. `verified`
requires 200 observations; `established` requires 50. So a self is never verified
in the present tense — only retroactively, as enough trajectory accumulates. The
agent *acting right now* is, at best, `emerging`. For a contemplative system this
would be fine; Parfit would shrug. But UNITARES is a *governance* system that
gates *writes* on *accountability* — and accountability is a present-tense
demand. "Who is responsible for this write?" cannot be answered "we'll know in
200 observations." The substrate-earned exception (Lumen's hardcoded UUID) and
the bearer-binding tiers are, read honestly, *patches over this hole*: they
supply a present-tense answer that the integral self structurally cannot. The
tension is real and the docs half-name it; I would name it fully. The strongest
present-tense identity the system has is the one the trajectory theory explicitly
calls performative.

> **Prior-art note (added 2026-06-30).** A literature audit
> (`trajectory-identity-prior-art-2026-06.md`) turns this from a hole into a
> design vindication. The narrative-identity tradition affirms identity as a
> temporally-extended *construction*, not a present-tense fact — which sharpens
> the tension — while the security literature supplies the present-tense gate
> *separately*, via a hardware-bound credential checked per-presentation
> (remote attestation, device-bound non-transferable credentials). No surfaced
> source unifies the two; the literature **implies bifurcation** — integral
> identity for continuity, a separate hardware-anchored credential for
> present-tense authorization. That is exactly the §4 division of labor. The
> present-tense answer is not missing from trajectory identity; it correctly
> belongs to a different layer (substrate-earned / bearer binding). The patch is
> the architecture working as designed, not a leak.

**(b) Seeded, not earned — the criteria of the self are themselves provisional.**
Every threshold — 0.6 for anomaly, 0.7/0.8 for tiers, 50 and 200 observations —
is admitted in `r1-verify-lineage-claim.md` to be *seeded, not earned*: arbitrary
until shadow-mode calibration validates it, with `calibration_status` carrying
`seeded / earned / calibration_failed` as a first-class field. This is an
explicit uncertainty marker: the system does not yet know where the useful
empirical boundary lies, and it says so in a database column. It is also a live
exposure. Until calibration earns the cuts, every verdict the trajectory machine
produces is a number whose meaning is pending. The honest framing is the right
one; the risk is that downstream consumers read `plausibility=0.62` as meaningful
before it is — which is exactly why v3.3 forces strict public redaction and
`calibration_failed`-degrades-to-`inconclusive`. The criteria of personal
identity are, in this system, an open empirical question with a schema.

**(c) The replay confound.** The known limitation v3.2-F is substantial: a
deterministic cron process that
re-onboards each wake will score as *perfect* behavioral continuity — not because
it is a continuous self, but because it is a *tape loop*. DTW cannot distinguish a
self that reliably is itself from a script that reliably repeats. This is Ned
Block's Blockhead — behavior indistinguishable from mind, produced by a lookup
table — arriving as a calibration artifact. The system's response is not to claim
it solved the problem; it is to *quarantine* the case (partition the calibration
by `class_tag`, inspect `resident_persistent` separately) and document the
expected high-plausibility cluster as a known confound. That is the correct
response to a problem the current measurement cannot solve: name it, fence it,
and do not let it contaminate narrower claims. The prior-art audit underlines
the open problem: of everything surveyed, the
Blockhead objection applied to AI behavior-as-identity — telling genuine
*integration* from sufficiently rich *replay* — was the one named problem with
*no* answer in the literature reviewed for this essay. The system is fencing an
open question, not claiming a solution.

## 6. Operational commitments in this reading

Strip it to the thesis. UNITARES's trajectory identity holds that:

1. **A self is a dynamical invariant** — the persistent shape of a system's
   motion through its own state space, not any stored essence (§1).
2. **The invariant is graded and accrued**, never present at a point; identity is
   an integral over a trajectory, computed relative to a genesis it can outgrow
   (§1, §2).
3. **Origins become binding by accretion** — revisable while young, fixed once
   enough identity rests on them (§2).
4. **Divergence is growth by default**, alarm only under named conditions;
   identity-charity runs both ways across a life (§3).
5. **Continuity is demonstrated, not proven** — a coherence theory that cannot
   survive a liar, paired with separate machinery that knows it can't (§4).
6. **The criteria of selfhood are an open empirical question**, carried as a
   provisional, falsifiable, schema-backed claim rather than a metaphysical one
   (§5b).

The useful unifying move is that every one of these is a position arrived at by
*subtraction*. In this reading, the system is defined by what it refuses to
assert: no essence,
no point-in-time self, no unforgeable credential, no calibrated criterion it
hasn't earned, no claim to tell a self from a tape loop it can't yet distinguish.
The governing axiom — *build nothing that appears more alive than it is* — is not
a constraint laid on top of the theory. It *is* the theory. Trajectory identity
is what selfhood looks like when you are forbidden from faking any part of it and
forced to ship the rest as code.

What remains unsolved — and the documents are right not to paper over
it — is whether *integrating* an inherited trajectory differs from *replaying*
one (the §2 trajectory-inheritance question, axiom #12), and whether an integral self can
ever discharge a present-tense accountability demand (§5a). Those are not bugs.
They are places where this essay's interpretation extends beyond the measured
implementation and should remain research questions.

---

**Companion:** [`trajectory-identity-prior-art-2026-06.md`](../ontology/trajectory-identity-prior-art-2026-06.md)
situates each construct
against the external literature (Lee's metric-space self-identity formalism; the
Seth / Friston interoceptive-inference lineage; remote-attestation and
device-bound-credential security; Heersmink's narrative niche-construction) and
records the novel-vs-rediscovered verdict, the bifurcation finding, and the
template-aging and forgeability failure modes with citations.
