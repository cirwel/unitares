- **Agent delivery:** follow-up review separates conflict resolutions from unrelated upstream work and compares resolutions to both merge inputs. `ship.sh --resume` joins existing review and CI evidence for an owned, pushed PR, applies readiness through the serial queue, and runs closeout without repeating commit/push or local full tests. Ownership, review-family, CI, and operator-reserved holds remain in force.

Operator-approved review refinement: ten total cycles now warn and request scope reassessment instead of blocking useful fix verification. The three-cycle redundancy budget, provider limits and required model-family coverage remain enforced.

Automatic delivery now requires a completed review result. Provider failures remain parked; an owning agent handles any ordinary advisory exception explicitly under the existing policy. Native review recovery and partial JSON findings retain their proper coverage state.
