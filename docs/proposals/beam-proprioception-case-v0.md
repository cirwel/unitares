# The Proprioception Case for the BEAM Footprint

**Created:** June 19, 2026
**Last Updated:** June 19, 2026 (v0.2 — reframed after a three-seat council pass; the v0.1 "two proprioceptions" taxonomy was rejected as a relabel and is replaced by an epistemic claim. See *What changed in v0.2*.)
**Status:** Draft v0.2 — conceptual companion to the committed A′ destination. **This is not a relitigation.** It argues no change to scope, sequencing, or the decision boundary already set by [`beam-footprint-roadmap-v0.md`](beam-footprint-roadmap-v0.md) (v0.3, A′ committed) and [`beam-governed-effects-dossier-2026-06-18.md`](beam-governed-effects-dossier-2026-06-18.md). It adds no surface and gates nothing.
**Relationship to existing docs:** read the footprint roadmap V0.3 RESOLUTION and the dossier's Decision Boundary first. This doc sits behind both.

---

## What changed in v0.2 (council fold)

A three-seat council (dialectic + code-review + live-verifier) reviewed v0.1. The substance verified; the *frame* did not.

- **Dialectic [B]:** v0.1's "two proprioceptions, one boundary" was a relabel of the roadmap's existing stateful-coordination/stateless-computation cut — it reclassified no surface, so it did no inferential work. **Folded:** the taxonomy is dropped. v0.2 makes an *epistemic* claim on a different axis (which self-reports are admissible evidence), not a taxonomic one.
- **Dialectic [B] + source check:** v0.1 called $V_{\text{anima}}$ "interoceptive." The paper (Wang 2026, §1.2, lines 161–173) **explicitly rejects** "interoception" on parsimony grounds and names the signal *proprioception*. **Folded:** "interoception" is struck entirely; this doc uses "proprioception" only in the paper's strict sense (a state-tracking signal — *what the signal does* — with no metaphysical claim).
- **Code-review [C]:** v0.1's "item for item" mapping dropped two Keep-in-Python items. **Folded:** the mapping claim is removed along with the taxonomy.
- **Code-review [C] / correction:** the `#18` citation is a real dispatch_beam PR (commit `f47d0e3`), but opaque cross-repo. **Folded:** cited explicitly below.
- **Live-verifier:** 4/5 runtime claims VERIFIED against the running system (sentinel-beam live pid 2259; lease plane :8788; orchestrator inert :8789; dispatch_beam live pid 78896). One DRIFT was in the *dossier* (`force-release` line refs), not here — noted for separate repair.

The council itself is the worked example of this doc's thesis (see §4).

---

## 1. The claim

Every prior round of this debate was fought on the **latency axis**, and the substrate keeps losing it honestly (PRs #350 / #354 / #360 / #361 / #533 — #533 collapsed 104× of user-visible overhead with one `run_in_executor`; the roadmap's V0.3.1 amendment and `feedback_substrate-migration-status-quo-bias.md` already concede this). This doc abandons the latency axis and argues one the latency rebuttals do not touch:

> **Honest, attributed runtime introspection is the closest a governed system gets to ground truth about itself — and a governance substrate should be built on the layer that can produce it.** A self-narrated state model can confabulate; an externally-observable runtime state is costlier to fake and costlier to fool yourself with. BEAM's edge is not speed; it is that its self-reports are *introspective* (PIDs, `:DOWN` monitors, supervision trees, Registry, ETS — structural facts about the running body) rather than *narrated*.

This is an axis the roadmap's stateful/stateless cut does not address. That cut decides *where computation lives*. This decides *which self-reports are admissible evidence* — a governance question, not a placement question.

## 2. Why this is not "more signal is always better"

The naive form — "BEAM emits more signal, more signal beats doubt" — is false, and **this migration already proved it false.** Dossier Evidence §2 / PR #846: BEAM harness telemetry is "operationally important but **analytically contaminating unless partitioned**." The ablation watchdog (`eprocess_eligible=1778`, `beam=1493`, `substrate=285`) showed BEAM rows would *masquerade* as EISV prior-state predictive signal; they are now **excluded by default** until tagged with `harness_lane`. There, more signal *raised* doubt until provenance resolved it.

So the law is conditional, and the conditions are the whole point:

> More **honest, provenance-tagged** signal lowers doubt. More **unattributed** signal raises it.

Two conditions convert volume into confidence:
1. **Honesty** — the introspection is not subverted (a compromised process can lie about its own state; this is the threat the `s19` attestation and `track-a` strict-identity work defend).
2. **Attribution** — the signal carries provenance (`harness_lane`, `effect_lane`, `verification_source`) so it cannot masquerade as a different kind of evidence (the #846 lesson).

BEAM's value is that it is high-volume *and* structurally introspective; #846 is the standing reminder that volume without attribution is contamination, not confidence.

## 3. The substrate already runs this thesis

This is not a new principle imported from outside — it is the operating logic of the existing system, just unnamed:

- **`verification_source: "external_signal"` → `externally_verified` corroboration grade.** A harness-observed outcome is graded *above* an agent's self-report precisely because external observation beats narration (dispatch_beam `governance.ex`; same pattern in UNITARES outcome ingestion).
- **The #846 `harness_lane` partition** is condition 2 enforced.
- **The `s19` attestation / `track-a` identity-hardening proposals** are condition 1 defended.
- **The dossier's Required Invariant 4** ("telemetry provenance: every emitted event includes lane tags") is this thesis written as a protocol rule.

The contribution of this doc is only to *name* the axis these already serve, so the A′ commitment reads as principled — built on "the layer that can introspect honestly" — rather than as the residue of where the last benchmark happened to land.

The relation to the proprioception paper is now clean: the paper claims "proprioception" (strict sense) for the durable trajectory-deviation signal $V_{\text{anima}}(t) = \int_0^t \lVert \mathbf{a}(\tau) - \boldsymbol{\mu_a}\rVert\, d\tau$ (defined in Wang 2026b, Trajectory Identity §6.1.1; imported by the paper's §2.2). Runtime introspection is the same *kind* of signal — honest state-tracking, "where my joints are" — at an immediate timescale rather than an integrated one. One family, two timescales; no second metaphysical category, and no "interoception."

## 4. The council as the worked example

This doc's own review demonstrates its claim. Three seats reviewed it: two (dialectic, code-review) reasoned over text and file-search — the *narrated* layer — and one (live-verifier) did runtime introspection (`ps`, `lsof`, `curl` against the live system). The introspective seat returned the cleanest, most ground-truthed verdict (4/5 VERIFIED, the lone drift not even in this doc). The narrated seats caught a real error the introspective one could not — that "interoception" contradicts the paper — *because that error lives in the text, not the runtime.* The division of labor is itself the thesis: **introspection is the privileged evidence for runtime-state claims; narration remains necessary for meaning-and-source claims; confusing the two is the failure mode.** v0.1's confabulated "interoception" was a narrated self-model drifting from its source, caught by a ground-truth check — in the very doc arguing ground-truth checks beat self-models.

## 5. Falsifier and discipline

- **Falsifier (not co-extensive with the roadmap's Stop Sign #1):** if a BEAM surface's *honest, attributed* introspection proves a *worse* evidence source than the Python path's self-reports — i.e., runtime state that is attributed yet still systematically misleads about what the system did — the thesis is wrong. (Stop Sign #1 is about contention/coordination *failure*; this is about *evidential quality* of self-reports. They can come apart: a surface can coordinate fine yet report its state misleadingly, or vice versa.)
- **Self-limiting:** this doc may not be cited to override a latency stop-sign, to accelerate a gated phase, or to reclassify a surface. It is an evidentiary frame, not a scope lever.
- **Bias, both poles, priced not just named:** `feedback_substrate-migration-status-quo-bias.md` warns this author resists migrations; the mirror risk is that a clean introspection narrative invites motivated reasoning. The discount this doc actually takes: it *concedes* the latency axis outright (§1), it *accepts* a documented counterexample to its own naive form (§2, #846), and it *survived* deletion of its v0.1 frame without losing the claim (§"What changed"). A frame that gives up its most attractive version and still stands is the one worth keeping.

## 6. Suggested disposition

A conceptual companion, not an RFC; moves no boundary. If accepted, the only mechanical changes are the existing one-line `proposals/README.md` index entry and (optionally) a back-reference clause from the roadmap's "Why migrate anyway, eyes-open" §1 noting the architectural-ceiling argument has an evidentiary framing recorded here. Separately, the live-verifier caught a stale line-ref in the dossier (`force-release` cited as `http_router.ex:205-227`; actual `:234-249`) — worth a one-line fix in that file, independent of this doc.

## References

- [`beam-footprint-roadmap-v0.md`](beam-footprint-roadmap-v0.md) — v0.3, A′ committed.
- [`beam-governed-effects-dossier-2026-06-18.md`](beam-governed-effects-dossier-2026-06-18.md) — the Decision Boundary, Evidence §2 (PR #846), Required Invariant 4.
- Wang, K. (2026). *Digital Proprioception and Allostatic Load* (`~/projects/digital-proprioception-paper/paper.md`), §1.2 — proprioception in the strict sense, and the explicit rejection of "interoception."
- `~/projects/dispatch_beam`: `PLAN.md` ("The ontology payoff"); `lib/dispatch/governance.ex` (`external_signal`); PR #18 / commit `f47d0e3` (the robustness audit: resume-failure recovery, active-run vs idle-reap distinction, snapshot write-amplification collapse) — the running existence proof, cited not depended on.
