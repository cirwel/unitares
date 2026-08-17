# Wave 3 — operator go-decision (§11 criterion 9)

**Status:** ⛔ **UNSIGNED DRAFT — NOT A DECISION.** Criterion 9 requires an
operator-authored artifact. Everything below except §1 is pre-filled from
measurement so the operator only has to supply the judgement; §1 is the
criterion's substance and **cannot be pre-filled by an agent** — it is
opportunity cost across the operator's own commitments.

**Criterion 9 verbatim (§11):** operator's `docs/proposals/wave-3-go-decision-<date>.md`
includes §"Calendar reasoning" naming current slip vs original target on each of
{paper, fellowship, HLH, R2 Phase 2}; no item slips >25% of original deadline
window. **No acceptance-memo escape.** Wave 1 elapsed time concretely named in
the document; (E)'s "× 3" cap derives from the actual measured Wave 1 elapsed.

---

## §1 Calendar reasoning — OPERATOR SECTION, DELIBERATELY BLANK

For each item: original deadline window, current target, slip as a percentage of
the original window. The criterion fails if any item slips more than 25%.

| Item | Original target | Current target | Slip (% of original window) |
|---|---|---|---|
| Paper (v7 / arXiv track) | | | |
| Fellowship | | | |
| HLH | | | |
| R2 Phase 2 | | | |

**Judgement:** _(operator)_

⛔An agent must not fill this table. Two of these four are the operator's own
career track, and "slip" here is a claim about intent, not a measurable.

---

## §2 Wave 1 elapsed — the (E) × 3 cap anchor

The criterion requires the cap to derive from **actual measured** Wave 1 elapsed,
not an estimate. Two readings, both stated because the choice between them
changes the cap and is itself a judgement:

| Reading | Span | ×3 cap |
|---|---|---|
| Labelled-commit span (verified 2026-08-16 from `git log origin/master`) | 2026-05-05 11:39:27 → 2026-05-06 02:40:39 = **~15 hours** | **~45 hours (~2 days)** |
| Start → Wave-1 close, as recorded by an earlier independent check | 2026-05-05 11:39 → 2026-05-07 01:12 = **~2 days** | **~6 days** |

⛔Neither is "~3 weeks", which is the estimate the v0.3 RFC used and which the
(E) disconfirmer was built on. Under either reading the cap is **days, not
weeks** — that is the load-bearing fact for §1, and it is why the original
10-prereq-PR plan was called structurally infeasible against this cap.

**Operator picks the reading:** commit-span (work actually performed) or
start-to-close (calendar the track occupied). _(operator)_

---

## §3 Gate state at the time of writing (2026-08-16) — context, not decision

Pre-filled from the §11.1 audit in `beam-wave-3-handler-dispatch.md`.

**Satisfied:** 1 (Wave 2 closed) · 4 (A.1 ODE math 0.8% of p99 — does not fire) ·
7 (C SDK spike run; dependency swapped to `anubis-mcp` v1.6.2).

**Open and blocking, both entry-shaped:**
- **8 (D state ownership)** — fired on a real 9th surface (UDS `peer_pid`
  attestation behind `core.substrate_claims`). The HYBRID answer exists only as a
  *recommendation* (`beam-wave-3-gamma-hybrid-v0.md` §0a REJECTED / §0c "recommend
  SHELVE"); §1 of the RFC still scopes identity middleware to BEAM. Ratifying the
  scope reduction would **shrink** Wave 3 and clear this halt.
  ⛔**This criterion cannot be closed by measurement.** (D) halts if the artifact
  is missing, **or** any 9th surface surfaces, or any of the eight is not
  re-derivable. Surface I has surfaced and the RFC's verdict on it is "Likely
  IRREDUCIBLE", so commissioning `docs/handoffs/wave-3-state-ownership-redteam-<date>.md`
  would **confirm** the halt, not open the gate. A concrete reduction is now
  drafted for signature in the RFC's **V0.6 SCOPE REDUCTION** section (identity
  out; one dialectic slice in) — ⛔unratified until §4 below is signed.
- **9** — this document.

**Exit criteria, correctly unmet:** 2, 3, 5 are evaluated after build, shadow soak
and cutover. ⛔Their `0 rows` today means *unmeasured*, not clean.

**Needs a ruling, not blocking:** 6 (A.2) is conditioned on a Python fix shipped
*during the implementation window*; no window ever opened, so it is arguably not
yet evaluable — though as written it halts because `process_agent_update` p99
improved to 795ms, which is the axis struck on 2026-06-24.

**NOT pinnable — corrected 2026-08-17.** 10 (F) was recorded here as "newly
pinnable" on a trailing-30d volume of **36**. That is the **raw** count and
criterion 10 may not be pinned from it. Measured against the live `governance`
database:

| Cohort | Resolved | Failed | Total |
|---|---:|---:|---:|
| Raw 30d (2026-08-17) | 15 | 23 | 38 |
| **Canary-excluded (2026-08-16)** | — | — | **18** |
| **Canary-excluded (2026-08-17)** | 9 | 10 | **19** |

Canary is **exactly half the raw rows** (19 of 38). Exclusion is
`core.agents.label NOT LIKE 'canary_dialectic%'` joined `a.id = d.paused_agent_id`.
⛔`trigger_source` cannot substitute: it is the literal `'manual'` on **all 38**
rows of this cohort. ⚠️Scope that claim correctly — table-wide the column holds
three values (`manual` 93, NULL 41, `loop_detection` 1), so "trigger_source is
useless" is true **of this window**, not of the column in general. The join is clean
— 0 null `paused_agent_id`, 0 orphans, 2 distinct statuses — so the shortfall is
real, not a join artifact.

**19 < 30, so criterion 10 remains halted on its own volume haltspec.** No
resolution-rate mean or σ may be pinned, and neither may the reviewer-reassignment
figures.

⛔**The window slides** — 18 → 19 in one day. Never quote a pinned `n` without its
as-of date.

⛔**Do not pin across #1705 — and it has now landed.** #1705 **MERGED
2026-08-17T08:08:53Z** (`f371e51f`, on `master`). It persists `synthesis_round`
and adds a genuine second synthesis round, changing what a `failed` row means. A
baseline pinned on pre-#1705 semantics cannot support the ≤5% regression
comparison the criterion requires. **The clean-cohort clock therefore starts at
the deploy of `f371e51f`, not at this document's date** — every row in the table
above predates the merge and none of them may seed the baseline.

⛔#1689 does **not** report the post-exclusion split; it reports the raw 36. Do
not cite it for 18.

**Arrival rate**, non-canary, by week: `07-27: 3 · 08-03: 5 · 08-10: 11`. The
07-06 → 07-20 zeros are the operator-absence window, not a rate.

⛔**Two different questions here; do not conflate them.**

1. *When does the running trailing-30d total reach 30?* At ~11/wk, about **one
   week** (≈2026-08-24; the oldest row does not age out until 08-26). ⛔**This
   number is not usable** — it counts pre-#1705 rows.
2. *When does a semantically clean post-#1705 cohort reach 30 from zero?* At
   ~11/wk, **≈2.7 weeks after `f371e51f` deploys**. This is the one criterion 10
   actually needs.

⚠️The weekly series is three points and the 08-10 week includes a five-row
single-day burst, so treat either date as a band, not a forecast.

---

## §4 Decision

⛔Blank by construction. GO / NO-GO / GO-WITH-REDUCED-SCOPE, signed and dated by
the operator. A "go" here does not waive criteria 2/3/5 — those still gate the
**close**, not the start.

_(operator)_
