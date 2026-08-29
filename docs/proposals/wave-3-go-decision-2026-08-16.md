# Wave 3 — operator go-decision (§11 criterion 9)

**Status:** ✅ **SIGNED 2026-08-22 — GO-WITH-REDUCED-SCOPE.**

⛔**Read against criterion 9 AS AMENDED 2026-08-22** (`beam-wave-3-handler-dispatch.md`
§11.9, PR #1822). The amendment landed **before** this signature by design: amending the
criterion afterwards would be retroactive reinterpretation, and that is what an earlier
draft of this artifact was rejected for.

§1, §2 and §4 carry the operator's judgement, supplied 2026-08-22. **§3 is a historical
snapshot dated 2026-08-16, preserved verbatim — read the ⛔ under its heading before
treating anything in it as current gate state.**

**Criterion 9 as amended (§11.9):** operator's `docs/proposals/wave-3-go-decision-<date>.md`
includes §"Calendar reasoning" naming current slip vs original target on each of
{paper, fellowship}; no item slips >25% of original deadline window. **No acceptance-memo
escape.** Wave 1 elapsed time concretely named in the document; (E)'s "× 3" cap derives
from the actual measured Wave 1 elapsed.

---

## §0 What this decision is, in the RFC's own terms

⛔**This artifact does not claim "the gate passes."** §0 closes: *if any disconfirmer fires
or any measurement source is missing at the gate, Wave 3 halts and the roadmap re-opens.*
(D) has fired. (F) is unpinnable. (C)'s artifact is missing on every ref. **Wave 3 as
originally scoped is halted, and this document does not lift that halt.**

What it does is execute the halt's own named exit. §0(E): *"Re-opening the gate requires
re-scoping Wave 3, not a written acceptance of the slip."* This signature is that
re-scope. "GO-WITH-REDUCED-SCOPE" is a term of art for exactly that act — not a euphemism
for clearing a halted gate, and not an acceptance memo, which §0(E) forbids.

---

## §1 Calendar reasoning — OPERATOR SECTION

Named set per criterion 9 **as amended**: {paper, fellowship}. The criterion fails if
either slips more than 25% of its original deadline window.

| Item | Original target | Current target | Slip (% of original window) |
|---|---|---|---|
| Paper (v7 / arXiv track) | ~Dec 2026, gated on the pre-registered 2026-12-01 stop-rule read | Unchanged — the 12-01 read date has not moved | **0%** |
| Fellowship | Schmidt Tier-1 2026-08-08 AoE; GovAI Scholar/Fellow 2026-08-16 AoE | Both closed — Schmidt submitted 2026-08-08, GovAI submitted 2026-08-15 | **0%** (each on or before its original date) |

**Removed from the named set by the 2026-08-22 amendment, not by this artifact:** HLH
(opportunity cost **zero — the track is dead**; no window was ever recorded and the event
produced no outcome) and R2 Phase 2 (evidence-gated, never date-gated; consumes no
operator calendar while deferred). ⛔Neither is recorded here as "not computable" — that
framing was the defect the amendment exists to close. Full reasoning lives in §11.9,
which is where a future reader must check it.

### Constructed to pass under both readings of (E)

⚠️§11.9 flags that §0(E) states the disconfirmer as a **conjunction** (projected
calendar-weeks > cap **AND** an item sacrificed) while the v0.3 fold states slip as a
**standalone unconditional halt**, and that a decision artifact must name which reading it
passes under. ⛔That reconciliation is an operator ruling and is **not made here.** This
artifact instead satisfies both:

- **Standalone-slip reading:** both named items slip **0%**. No halt.
- **Conjunction reading:** the left-hand term is also required. The work this signature
  authorises is bounded in §4 to *authoring the smaller §11-style gate document and its
  design pass*, projected at **~15–25 hours**. ⚠️**That projection is an agent estimate,
  not an operator input**, recorded for ratification. Against the §2 cap of **~112h38m**
  it clears with wide margin under any plausible revision. ⛔If the operator rejects the
  estimate, this section must be re-read before the gate is cited.

Because both terms hold, naming the reading is not load-bearing **for this decision**.
⛔It remains owed before any future gate read.

**Judgement:** _(operator, 2026-08-22)_ — Nothing in {paper, fellowship} is being
sacrificed to authorise the reduced-scope gate and design work. Both carry real deadlines
and neither has slipped. The re-scope in §4 is the RFC's own named exit from a halt that
has already fired, and taking it now costs less than leaving Wave 3 halted and unscoped.

---

## §2 Wave 1 elapsed — the (E) × 3 cap anchor

The criterion requires the cap to derive from **actual measured** Wave 1 elapsed.

⛔**Corrected 2026-08-22.** An earlier draft ended the labelled-commit span at #384
(`2026-05-06 02:40:39`). That endpoint is wrong under every inclusion rule — **five
further `wave-1`-prefixed commits follow it on `origin/master`**, and the same Sentinel
work continues in `fix(sentinel)` / `feat(sentinel-summary)` commits after that. No cap
derived from it is defensible.

| Reading | Span | ×3 cap |
|---|---|---|
| Literal `wave-1`-prefixed subjects only (`a686531b` → `b33f7972` #394, `2026-05-06 07:09:57`) | ~19h30m | ~58.5 hours |
| **CHOSEN — all Wave-1 Sentinel work** (`a686531b` `2026-05-05 11:39:27` → `aa75ec90` #404, `2026-05-07 01:12:12`) | **37h32m45s** | **~112h38m (~4.7 days)** |

**Operator picks the reading:** _(operator, 2026-08-22)_ — **all Wave-1 Sentinel work.**
The `fix(sentinel)` / `feat(sentinel-summary)` follow-ups (#398, #399, #404) are the same
Wave-1 work under a different commit prefix; excluding them on prefix alone would be
arbitrary. **Wave 1 elapsed = 37h32m45s; the (E) cap is ~112h38m.**

⛔**Neither reading is "~3 weeks",** the estimate the v0.3 RFC used and on which the (E)
disconfirmer was built. Under every reading the cap is **days, not weeks** — the
load-bearing fact behind calling the original ten-prereq plan structurally infeasible.

⛔**This supersedes the roadmap's 2026-05-09 pin.** `beam-footprint-roadmap-v0.md` pinned
Wave 1 elapsed at ~2 days and its option (δ) calls that "the corrected ~2 days"; that pin
is replaced by the measurement above. ⚠️The roadmap's finding at `:343` — *"the calendar
gate is structurally infeasible at current Wave 1 measurement"* — is **not** contradicted
by §1's pass: `:343` is a finding about **the ten-PR prereq stack**, which V0.6 retires.
It would still hold against that stack today.

⛔**"Commit-span" is calendar elapsed between two commits, not labour.** §14 states the
cap's unit as calendar-weeks. An earlier draft glossed this reading as "work actually
performed"; that gloss is withdrawn — it silently changed what the cap measures and was
not the operator's word.

---

## §3 Gate state at the time of writing (2026-08-16) — HISTORICAL SNAPSHOT, not current state

⛔**PRESERVED VERBATIM AS A DATED SNAPSHOT. DO NOT READ AS CURRENT GATE STATE.** Three of
its claims were superseded within days of writing; it is kept unedited only so the record
shows what was believed on 2026-08-16:

1. ⛔Its prediction that commissioning the state-ownership red-team *"would **confirm** the
   halt, not open the gate"* was **withdrawn by the RFC on 2026-08-17**. It predicted what
   an independent lane would conclude by reading the very section that lane exists to
   check independently. Surface I is a **candidate** 9th with a hedged verdict, and
   criterion 8's text is a *reducibility* test, not a count test. See §4.
2. ⛔Its criterion-10 figures are computed off raw `status` and are superseded — see §4 for
   the classifier-based numbers, which are less than half as large.
3. ⛔Its ⛔ about #1689 not reporting the post-exclusion split was true on 2026-08-17 and is
   now stale; §11.10's 2026-08-19 entry attributes that split to #1689.

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
---

## §4 Decision

**GO-WITH-REDUCED-SCOPE.** _(operator — Kenny Wang, 2026-08-22)_

### What this signature authorises

**It ratifies the RFC's V0.6 SCOPE REDUCTION** — identity middleware is **out** of Wave 3
indefinitely (Python remains sole owner of the entire identity-resolution and
authorization transaction), the (γ) handler-dispatch cut stays retired in both shapes, and
the dialectic leg's stuck-session work is **in**.

⛔**It does NOT authorise an implementation start.** RFC V0.6: *"Signing V0.6 authorises
the scope, not an implementation start; the smaller gate is owed first."*
`beam-wave-3-gamma-hybrid-v0.md` §6 requires a re-scope to re-open the disconfirmer gate at
smaller size with its own §11-style gate, and **V0.6 supplies none** — no disconfirmer set,
no exit criteria, no re-derived (B) boundary budget, no stop-sign revision.

**Bounded authorisation** — ⛔the criterion-9 apparatus is an opportunity-cost cap, so the
work it releases must itself be bounded:

| | |
|---|---|
| **Deliverable** | one §11-style gate document for the reduced scope, plus its design pass and council review |
| **Projection** | ~15–25 hours (⚠️agent estimate, awaiting operator ratification) |
| **Counts against the §2 cap?** | **Yes** — ~112h38m, so this consumes ≈13–22% of it |
| **Clock starts** | at this signature, 2026-08-22 |
| **Completion artifact** | the gate document, operator-signed, as criterion 9 was |

⛔Building begins only after that gate exists and is met. ⛔No implementation PR may cite
this signature as its authority.

**Gate document drafted 2026-08-29 (unratified):** `docs/proposals/wave-3-reduced-scope-gate-v0.md`.
It proposes scoping to path (1) only, rederives criteria 2/3/5 and the (B) budget for a periodic
topology, and reports one blocking prerequisite — the sweeper's guard-refusal count
(`skipped_count`) reaches no durable channel, so the dual-writer hazard this scope exists to repair
is unobservable today. ⛔It is a proposal awaiting operator signature, not a met gate.

### The scope, stated exactly — it is TWO runtime paths, not one

⛔**An earlier draft presented five symbols as a single slice. They are not call-closed.**
Verified against `master` 2026-08-22: `auto_resolve_stuck_sessions` calls **none** of
`check_reviewer_stuck`, `check_timeout`, or `_apply_reviewer_reassignment`.

| Path | Members (verified 2026-08-22) | Character |
|---|---|---|
| **(1) Periodic sweeper** | `auto_resolve_stuck_sessions` (`src/mcp_handlers/dialectic/auto_resolve.py:102`), `_parse_timestamp` (`:32`) | the dual-writer hazard; the reason to take this at all |
| **(2) Request-driven** | `check_reviewer_stuck` (`src/mcp_handlers/dialectic/handlers.py:211`), `_apply_reviewer_reassignment` (`:933`), `check_timeout` (`src/dialectic_protocol.py:1216`) | get / takeover / reassign entry points |

⛔**The smaller gate must estimate and gate these separately, or scope only (1) plus its
dependencies.** ⛔Line numbers drift — re-verify by symbol, never by line. V0.6's own
numbers are stale and are deliberately not reproduced.

**Why take it:** the sweeper writes to PostgreSQL outside BEAM ownership and outside the
reserve-first serialization, and `has_inflight_saga_async` (`auto_resolve.py:163`) is
checked once-early with several DB round-trips before the status write. ⚠️**Not "without
guards"** — #1804 added terminal-state write-tail predicates
(`AND status NOT IN ('resolved','failed')`) to `update_session_reviewer` and
`update_session_status` (`src/dialectic_db.py:283`, `:322`), and the callers check the
return. Those narrow the window; the ownership gap is what remains.

⛔**Do not restate this scope as "the reserve-first serialization point."** **(B)
reserve-first** is defined for **Invariant 5, `SYNTHESIS→RESOLVED` serialization**. The
RFC's write table separately *asserts* (B) as the fix for the reassignment writes, but (B)
as specified does not cover them. ⛔Whether reserve-first extends to the reassignment path,
or that path needs its own serialization design, is an open question the smaller gate must
settle.

⚠️**Unresolved cap risks:** `_apply_reviewer_reassignment` has **3 call sites**
(`handlers.py:1589`, `:2508`, `:3138`) so its blast radius exceeds "stuck-session";
`check_timeout` is not trivial arithmetic; `auto_resolve.py:187` calls `select_reviewer`
(`src/mcp_handlers/dialectic/reviewer.py`, 450 lines, `contextvars` reentrancy guard at
`:36-38` that works only because everything runs in one Python process) — unscoped and
unestimated; and the scope **reads** `mcp_server.agent_metadata`, §3.1 surface G, whose
port strategy is §10, which V0.6 scopes out.

### Criterion 8 is DISSOLVED for this scope, not cleared

Per RFC V0.6, removing identity middleware means *"(D) — a gate about the
identity-middleware port — **has no subject**."* ⛔This ratification therefore **dissolves**
criterion 8 for the reduced scope. It does not measure it clean, and it does not lift the
halt on the original scope, which stands.

⛔The (D) halt on the **original** scope is satisfied by its first clause alone — no
`docs/handoffs/wave-3-*` artifact exists on any ref (verified two ways 2026-08-22:
`git log --all` across 245 remote branches, and a GitHub API 404). ⛔Adjudicating that
contradiction is **historical cleanup, not a prerequisite** for the reduced scope, and is
listed non-gating below. ⛔Committing the `-prep-` worksheet (`:305`) removes **no** halt
clause — v0.3.5 calls it prep, not the red-team.

### ⛔What this does NOT waive

- **Criteria 2, 3 and 5 cannot stand verbatim and must be rederived.** ⛔An earlier draft
  said they "still gate the close" unchanged. Criterion 2 measures **handler dispatch**,
  which V0.6 excludes; V0.6 states the reduced scope generates **zero rows** for criteria 3
  and 5. Equivalent post-build evidence remains mandatory — the smaller gate must
  **replace or rederive** these three, not inherit them.
- **Criterion 6 needs an operator ruling and does not have one.** §3 flags it as "worth an
  operator ruling either way"; as written it halts *because* `process_agent_update` p99
  improved to 795ms, the axis struck on 2026-06-24. ⛔Left unresolved deliberately rather
  than ruled by an agent — an open halt inside a signed GO is recorded, not hidden.
- **Criterion 7 has the same missing-artifact hole as criterion 8.** §3 records it
  Satisfied, but its §11 source `docs/handoffs/wave-3-mcp-sdk-spike-<date>.md` exists on no
  ref, and §11's preamble says *"If any source is missing at gate, gate halts (no fallback
  default)."* ⛔Applying that clause to 8 and not to 7 is inconsistent. It does not fire on
  the merits (anubis-mcp v1.6.2 is current), but the artifact is owed.
- **Criterion 10 is not pinnable and must be pinned before implementation.** ⛔It must be
  computed through `src/dialectic_outcomes.py::classify_outcome` /
  `DialecticDB.get_outcome_breakdown()`, **never off raw `status`** — §0(F) says so in ⛔
  terms. Measured live 2026-08-22, canary-excluded:

  | Cohort | resolved | failed | unresolved_awaiting_facilitation | open | **eligible (resolved+failed)** |
  |---|---:|---:|---:|---:|---:|
  | Post-#1705 (created ≥ `f371e51f` merge, 2026-08-17T08:08:53Z) | 5 | **0** | 6 | 1 | **5** |
  | Trailing 30d (spans #1705 — ⛔unusable) | 14 | **0** | 16 | 1 | **14** |

  The eligible cohort is **5 against a floor of 30**. ⛔An earlier draft reported 11 and 30
  by counting `status='failed'` rows; every one of those is a standing unfacilitated
  objection — the dialectic working as intended, and precisely the conflation #1689
  retired. ⛔5 is an **upper bound**: the clean clock starts at *deploy*, and no
  deploy-audit record pins when `f371e51f` first went live, so the true figure can only be
  bounded. ⛔Use `created_at`; `updated_at` more than doubles the cohort by sweeping in old
  sessions merely touched recently. ⛔The window slides — never quote these without the
  as-of date.

  ⛔**Three further reasons criterion 10 is not merely under-volume:**
  1. **`failed = 0` across all non-canary traffic**, so the resolution rate is 1.0 with
     binomial σ = 0. A "≤5% regression vs mean + σ" test then fires on a **single** failure.
     ⛔Reaching n=30 may not yield a usable baseline; the smaller gate needs a fallback
     quality criterion.
  2. **The reassignment stream is incomplete.** `_apply_reviewer_reassignment` emits
     `dialectic_reviewer_reassigned` (`handlers.py:1021`); the sweeper writes reviewer
     changes directly and emits **nothing**. ⛔The ≤20%-increase test cannot be pinned on the
     window accrued to date. Corrected in §11.10 (PR #1822).
  3. **The canary filter is necessary but not sufficient.** `label NOT LIKE
     'canary_dialectic%'` leaves synthetic and tooling sessions in the cohort. ⛔The smaller
     gate must define an organic-session predicate first.

  ⚠️**Denominator disagreement, unresolved:** §0(F) defines it as
  `resolved + failed + escalated`; §11.10 and `resolution_rate()` use `resolved + failed`
  with no escalated term. ⛔Resolve before pinning.
- **The γ cut at `process_agent_update` stays retired** — a retired lever, not a retired
  goal.
- ⛔**The parity family** (`canonical_payload`, `compute_signature`, `Resolution.hash`, all
  in `src/dialectic_protocol.py`) stays **excluded**. Verified 2026-08-22 that the scope's
  call graph does not reach it; the bare-`::jsonb` double-encode already wrote 90 unreadable
  rows between 2026-06-28 and 2026-08-10.
- ⚠️`check-wave3-ode-prereq.sh:24-27` engages only when `elixir/handler_dispatch` **exists**;
  absent, it exits 0 with "gate not engaged (pass)". That tree stays absent for this scope,
  so the §14 guard never fires. Decide deliberately whether §14 binds rather than letting
  the lint's shape decide.

### Owed next, in dependency order

**Gating — implementation may not start until all four are done:**

1. **Close the scope.** Decide one path or two (above), achieve call-graph closure, and
   estimate `select_reviewer` and the `agent_metadata` / surface-G ownership question. Rule
   on §14. Rule on criterion 6.
2. **Start the clean criterion-10 clock and fix its instruments** — route the sweeper
   through the reassignment chokepoint (or emit from it) and restart that window; define the
   organic-session predicate; resolve the escalated-term denominator disagreement. ⛔This is
   the **longest pole**: at the eligible-cohort accrual rate this is roughly **4.5–9 weeks**
   to n=30, not §3's "≈2.7 weeks" (computed on all non-canary sessions, most of which never
   enter the denominator).
3. **Author the smaller §11-style gate** — disconfirmers, exit criteria, re-derived (B)
   boundary budget, stop-sign revision, replacements for criteria 2/3/5, the
   reserve-first-vs-reassignment question, and the lower-blast-radius reader-fix alternative
   #1689 itself prefers. Takes (1) and (2) as inputs.
4. **Pin criterion 10** on a window entirely after `f371e51f`'s deploy, ≥30 canonical
   resolved+failed outcomes, means **and** σ pinned before implementation.

**Non-gating — historical cleanup, parallel:**

5. Adjudicate the criterion-8 artifact contradiction and the criterion-7 missing spike
   artifact. ⛔Not a prerequisite: once identity is out, (D) has no subject for the reduced
   scope. ⛔If pursued, pair it with the surface-I ruling — clearing the missing-artifact
   clause alone converts a clean halt into a contested one.
6. Ratify or revise the §1 projection (~15–25h), and rule on the §0(E)
   conjunction-vs-standalone reading so a future gate read does not inherit the ambiguity.

---

## Provenance

⛔**The operator supplied exactly five judgements on 2026-08-22.** Nothing else in this
document is an operator input:

1. **GO-WITH-REDUCED-SCOPE** (§4).
2. **The paper target is unchanged** — ~Dec 2026 / the 12-01 read holds (§1).
3. **Commit-span** rather than start-to-close, as the §2 reading.
4. **Amend criterion 9 first, then sign** against the amended text.
5. **All Wave-1 Sentinel work** as the §2 inclusion rule — 37h32m45s, cap ~112h38m.

Everything else — the fellowship dates, the removed-item reasoning, the scope analysis,
every measurement, the sequencing, and the ~15–25h projection — is agent transcription,
measurement, or derivation, recorded for operator ratification. ⛔An earlier draft claimed
the operator supplied *every* judgement in §1, §2 and §4. That was false and is withdrawn.

**Review provenance:** five independent adversarial reviewers on 2026-08-22 — two
code-focused, one architectural, one runtime-verification, one heterogeneous model. The
governed dialectic reviewer **rejected** the previous draft (`agrees=false`); that
rejection was accepted rather than argued, and this rebuild is the response to it.
