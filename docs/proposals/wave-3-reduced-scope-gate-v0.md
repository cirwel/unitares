# Wave 3 reduced-scope gate (v0) — the smaller gate owed by the 2026-08-22 signature

**Status:** ⛔ **PROPOSED — UNRATIFIED as a gate.** The document as a whole still awaits operator
signature and the council round §8 records as unheld.

⚠️**But four of its open questions are now closed.** On 2026-08-29 the operator ruled on §6.1
(reassignment gets its own serialization design), §6.2 (criterion 6 retained, halt authority
removed), §6.6 (**instrument first — build the §3.1 emitter, decide the port on what it reports**)
and, consequentially, §6.5 (deferred, with a named reopen condition). ⛔Those rulings are settled;
the rest of this document is not. §6.3 and §6.4 remain owed.

⚠️**Implementation correction after merge.** PR #2011 supplied a positive-only
`dialectic_write_refused` incident event. It did not supply a cycle denominator, did not count the
early saga-inflight skips, and cannot observe the opposite ordering where the sweeper writes before
a saga starts. The follow-up after #2008 adds a zero-inclusive `dialectic_sweep_cycle` event and
puts both periodic and lazy resolver entry points under one task-local reentrancy flag. Those
changes improve the instrument; they do **not** make the full dual-writer hazard measurable and do
not start the §7 window.

**What it discharges.** `wave-3-go-decision-2026-08-16.md` §4 signed GO-WITH-REDUCED-SCOPE and
named exactly one released deliverable: *"one §11-style gate document for the reduced scope, plus
its design pass and council review."* It also said, twice, that the signature **does not authorise
an implementation start** and that *"building begins only after that gate exists and is met."*
This is the first half of that deliverable — the gate document. The design pass and council
review are not in it and are not claimed.

**What it does not do.** It does not authorise building. It does not clear, lift or re-read the
(D) halt on the original scope. It does not rule on the operator questions §6 lists as owed. A
gate that cleared itself is the defect the go-decision artifact was rejected for once already.

**Clock.** The §4 authorisation started 2026-08-22 and projected ~15–25h against the §2 cap of
~112h38m. This document is drafted 2026-08-29.

---

## §1 The scope this gate covers — re-verified by symbol

⛔The go-decision says *"line numbers drift — re-verify by symbol, never by line."* **Every one of
its five line numbers had drifted by 2026-08-29**, seven days after signature. Re-verified against
the working tree at `bf8ce77`:

| Path | Symbol | File | Signed 2026-08-22 | 2026-08-29 (`bf8ce77`) | 2026-08-29 (`83efd0b`) |
|---|---|---|---|---|---|
| (1) sweeper | `auto_resolve_stuck_sessions` | `src/mcp_handlers/dialectic/auto_resolve.py` | 102 | 143 | **143** |
| (1) sweeper | `_parse_timestamp` | `src/mcp_handlers/dialectic/auto_resolve.py` | 32 | 37 | **37** |
| (2) request | `check_reviewer_stuck` | `src/mcp_handlers/dialectic/handlers.py` | 211 | 249 | **252** |
| (2) request | `_apply_reviewer_reassignment` | `src/mcp_handlers/dialectic/handlers.py` | 933 | 1028 | **1031** |
| (2) request | `check_timeout` | `src/dialectic_protocol.py` | 1216 | 1224 | **1245** |

`_apply_reviewer_reassignment` call sites: signed as 1589/2508/3138, then 1706/2681/3485, now
**1749/2753/3561**. The **count** of three is stable; the blast-radius finding stands.

⚠️**The last two columns are seven days and roughly two hours apart.** The middle column was
verified while drafting this document; `master` then advanced 48 commits — among them #1993, a
dialectic fix — and three of the five symbols moved again before the draft could be committed.
⛔The rows that did *not* move are the ones in files that commit range never touched
(`auto_resolve.py`, `reviewer.py`, `background_tasks.py`), which is the mechanism, not luck.

This is recorded as a property of the artifact, not as a complaint: a scope defined by line number
decays faster than the document defining it can be written. **This gate defines its scope by symbol
and call-graph reachability only**, and any future restatement should do the same. ⛔The table above
is illustration of the decay, not an index to navigate by — re-derive by symbol.

### §1.1 Proposed reduction: gate path (1) only

The go-decision §4 offers a choice — *"estimate and gate these separately, or scope only (1) plus
its dependencies."* **This document proposes: scope only (1).** The reasoning is not cost:

- Path (2)'s centrepiece `_apply_reviewer_reassignment` has **three call sites**, none of which is
  the stuck-session subject. Porting it moves a shared mutation helper used by get / takeover /
  reassign entry points, so the change's blast radius is the whole reassignment surface, not the
  stuck-session hazard. That is a *different* port wearing this one's authorisation.
- Path (1) alone is where the stated justification lives. The sweeper is the second writer; its
  three mutations have terminal-state predicates at the write tail, but no shared reservation
  across the earlier saga check and the later write. Path (2) already routes BEAM-first with
  Python fallback under `UNITARES_DIALECTIC_BEAM_RESOLUTION`.
- `auto_resolve_stuck_sessions` calls none of path (2)'s three mutation symbols. That permits the
  mutation scope to be split, but it does **not** make the resolver a periodic-only island: the
  same function is also entered lazily from `is_agent_in_active_session`, including request-driven
  reviewer selection. The exposure and boundary budget must count both trigger classes.

⛔If the operator prefers to keep (2), this gate does not cover it and a separate estimate is owed.

### §1.2 The unscoped dependency, stated as the central design question

`auto_resolve_stuck_sessions` calls `select_reviewer` (~450 lines: authority scoring, cooldown,
capability filtering), and `select_reviewer` calls `is_agent_in_active_session` once per candidate.
That check can lazily enter the resolver again. Before the post-#2008 correction, its
`ContextVar` was set only by the lazy caller, so a direct/background resolver invocation entered
with the flag clear and could fan out to one nested sweep per candidate. The shared entry-point fix
now makes the resolver own the flag for the full invocation.

⛔The `ContextVar` is a task-local **reentrancy flag**. It is not a critical-section lock, does not
serialize concurrent Python processes, carries no reviewer-selection semantics, and cannot
coordinate with BEAM. The go-decision's unscoped selector dependency therefore remains, but its
risk must not be described as porting an in-process lock. Three options, with a recommendation but
no ruling:

| Option | Shape | Cost | Risk |
|---|---|---|---|
| **(a) Callback** | BEAM owns the sweep timer and the write; calls back into Python for each reviewer decision | smallest port | adds at least one boundary crossing **per decision**, straight into (B)'s budget; the callback must suppress Python's lazy resolver entry |
| **(b) Carry `select_reviewer`** | port the selector too | largest port | ~450 lines of scoring logic is a parity surface, and parity surfaces on this RFC have a bad record; the task-local reentrancy flag itself is not a portable serialization primitive |
| **(c) Split the timer from the decision** | BEAM owns *detection and serialization* (which sessions are stuck, who may write); Python keeps *selection* and calls BEAM to reserve before writing | smallest **semantic** change | reservation, and likely selection handoff, are per-decision boundary operations unless explicitly batched; requires the reassignment serialization design §6.1 reserves |

⚠️**Recommended: (c), conditional on §6.1.** It is the only one that addresses the stated hazard —
ownership of the write — without porting a scoring surface nobody asked to move. It does **not**
avoid per-decision boundary cost; §3.2 must budget that honestly unless the protocol batches it.
⛔Under the settled §6.1 ruling, (c) needs its own reassignment serialization design rather than
inheriting the RFC's reserve-first mechanism.

⛔**Estimate withheld deliberately.** All three options are unestimated. An estimate that named
hours before §6.1 is settled would be an estimate of an undecided design, and the §2 cap deserves
better than a number invented to fill a cell.

---

## §2 Falsifying-evidence question, restated for this scope

> **What evidence would update us away from moving stuck-session detection and reassignment
> serialization to BEAM?**

⛔The original §0's disconfirmers do not transfer verbatim, and the reason is topological, not
cosmetic. (A) and (B) were written against a **per-request** port (handler dispatch): their
thresholds are per-call costs measured against per-call baselines. The reduced resolver has two
live trigger classes: a periodic 10-minute task and a lazy call from
`is_agent_in_active_session`, whose volume follows review/selection traffic. A design that calls
Python for selection or BEAM for reservation can add another per-decision crossing inside either
trigger. One 144-cycles/day denominator therefore does not describe this workload.

### Proposed reduced disconfirmer set

Each is a **proposed prior**, stated as a choice before it is applied, per the measurement-authority
rule. None has been measured. Every measurement cell below is deliberately empty.

**(b1) The hazard is not real in production.** Over a 30-day window, a complete overlap instrument
records zero collisions in **both** writer orderings, every expected periodic cycle is present,
and every lazy invocation is attributed separately. A zero `dialectic_write_refused` count alone
does not fire this disconfirmer: it sees the "other writer won first" ordering but misses a saga
that starts after the early check and loses to a successful sweeper write.

**Measurement source:** ⛔**INSTRUMENTED, NOT YET MEASURED — see §3.1.** All three orderings now
emit: `saga_inflight_skip_count` (BEAM first, caught at the early check),
`dialectic_write_refused` (BEAM first, caught at the write), and `dialectic_write_overlap`
(sweeper first, caught by a post-write probe), each denominated by `dialectic_sweep_cycle`.
⛔**This does not make (b1) firable on a clean window.** Three conditions are unmet:

1. ⛔**No window has started.** Deployment provenance is still owed — §7 step 3, slot empty.
2. ⛔**The probe bounds the interval rather than closing it.** A saga starting after the probe is
   unobserved, so a clean window is evidence over a bounded interval. Firing (b1) means accepting
   that bound as sufficient — an operator judgement, not a measurement result, and one that should
   be stated as a choice before the window is read rather than after.
3. ⛔**`overlap_probe_failed_count` must be read beside the detected count.** A window with
   non-trivial probe failures has not measured overlap regardless of how many zeros it shows.

**(b2) The in-place fix closes it.** A Python-side change during the implementation window makes
the sweeper's write path safe without porting — e.g. an explicit row/advisory lock or a shared
reservation honored by **both** writers across the saga check and status write. Merely putting the
two statements in one ordinary PostgreSQL transaction does not close the TOCTOU window: at normal
isolation, another transaction can still insert/start the saga between them. If a real
serialization primitive lands and holds for 30 days under complete overlap telemetry, the port is
redundant. **Measurement source:** same channel as (b1), same prerequisite.
⚠️This disconfirmer is the analogue of the original (A.2), and it is **more likely to fire here
than (A.2) ever was**: the reduced scope's hazard is a serialization bug, and serialization bugs
have in-process fixes. ⛔Naming that honestly is the point of a disconfirmer set.

**(b3) Boundary cost exceeds the coordination bought.** ⛔**Deliberately left without a numeric
threshold — see §3.2.** The original (B)'s ×2 / ×3 multipliers against lease-plane Phase A
(resident surface: p50 28ms, p99 5,146ms, n=44,535, window 2026-05-06→05-20) were derived for
per-request marshalling. They cannot be inherited. §3.2 proposes what replaces them and why the
number is the operator's to set.

**(b4) Reentrancy or selection semantics prove irreducible.** The port surfaces selection state
that cannot be preserved across the proposed boundary, or its callback/handoff causes the Python
lazy resolver to re-enter. The `ContextVar` itself is only the known task-local recursion
suppression mechanism; it is not evidence of locking or an irreducible selection semantic.
**Measurement source:** design pass on §1.2's chosen option, before build. ⛔This is the reduced
scope's structural analogue of (D), and unlike (D) it has a subject.

**(b5) Opportunity cost.** Unchanged in form from §0(E) and inherited as-is, with the §2 cap
(~112h38m) as the ledger. ⛔The §11.9 conjunction-vs-standalone ambiguity is **not** resolved here;
it is an operator ruling and is listed in §6.

⛔**Not carried forward, with reasons:** (A.1)'s ODE-floor gate (its subject is
`process_agent_update` p99, an axis struck 2026-06-24 and belonging to a port this scope excludes);
(C)'s MCP SDK gate (the sweeper is not an MCP transport surface — ⚠️but see §6.4, the owed
artifact is a separate debt and is not discharged by irrelevance here); (D) (dissolved for this
scope by the signature, not cleared).

---

## §3 What must exist before this gate can be read

### §3.1 ⛔PREREQUISITE: all three orderings are now instrumented; the interval is bounded, not closed

**This remains the gate's central prerequisite and blocks disconfirmers (b1) and (b2).** The
instrument now has two complementary pieces:

- PR #2011 added `dialectic_write_refused` at the three guarded write sites
  (`reviewer_reassignment`, `awaiting_facilitation`, `reap_failed`) and retained `skipped_count` in
  the periodic log. A row is positive evidence that the sweeper attempted a write and the
  terminal predicate refused it.
- The post-#2008 follow-up attempts one `dialectic_sweep_cycle` row for every real resolver
  invocation, including all-zero cycles. Audit writes are fail-soft, so an unavailable audit sink
  leaves a heartbeat gap rather than blocking maintenance. A present row records `trigger_source`
  (`periodic`, `active_session_check`, or an explicit direct caller), scanned active/stuck counts,
  whether the 100-row maintenance batch was truncated, invalid rows, early saga-inflight skips,
  guarded write attempts/refusals, outcomes, duration, and errors. The sweeper requests
  least-recently-updated rows first and fetches one overflow sentinel, so a full batch cannot
  silently masquerade as a table-wide denominator or continually hide the oldest stuck rows behind
  newer activity. A reentrant call suppressed by the shared `ContextVar` emits no cycle because it
  performed no scan. If a later row aborts a cycle after earlier writes committed, the error-bearing
  cycle preserves those earlier outcome counts rather than reporting a false all-zero result.

Together those events distinguish "the producer ran and observed zero guarded refusals" from "no
producer evidence exists." Coverage is still a predicate, not an assumption: a periodic window
must account for the expected fixed-delay heartbeats and treat any unexplained gap as missing
evidence, while lazy cycles are denominated by their own emitted rows rather than by 144/day.

The three dual-writer orderings, and which of them is now observed:

- An early `saga_inflight_skip_count` sees BEAM already owning the session when Python checks.
- A `dialectic_write_refused` row sees another writer finish before Python's guarded write.
- ✅**The sweeper-first ordering is now instrumented.** A `dialectic_write_overlap` row is emitted
  when a probe taken immediately after a *successful* guarded write finds a non-terminal saga the
  early check did not. This was the case the previous revision recorded as seen by nothing.

⛔**That third instrument narrows the unmeasured interval; it does not empty it.** The probe closes
the window between the early check and just after the write. It cannot close the window after
itself, so a saga starting later is still unobserved. **A zero overlap count is therefore evidence
about a bounded interval, not proof of a collision-free system.** Only a serialization primitive
both writers honour removes the interval instead of measuring it — that is (b2), still unbuilt, and
the instrument-first ruling does not authorise it.

⛔**A failed probe is not an observed absence, and the counts are separate for that reason.**
`has_inflight_saga` fails open — correct for a write gate, since no saga infrastructure means
nothing to race — and an instrument inheriting that would report "no saga" when it could not look.
`probe_inflight_saga` returns `None` in that case, and the cycle event carries
`overlap_probe_failed_count` beside `overlap_detected_count`. ⛔Read the pair or neither: a window
with a non-trivial probe-failure count has not measured overlap, whatever its detected count says.

⚠️A positive refusal can still also mean a missing row or a competing Python writer; the DB helper
returns one `False` for all of them and only logs the distinction. That ambiguity is unchanged.

Therefore a zero refusal count, even with complete cycle coverage and complete probe coverage,
remains a measured zero over a bounded interval rather than proof the hazard is absent. No §7
window starts before probe coverage and deployment provenance are both present.

⚠️The reassignment-success emitter remains useful for the separate historical metric gap, but the
guard-refusal/cycle streams measure coordination. They are not a substitute measure of reviewer
reassignment behavior; see §4, R3.

### §3.2 The (B) boundary budget must be re-derived, and its denominator changes

⛔**Do not inherit the ×2 / ×3 multipliers.** They compare a per-request payload against a
per-acquire lease ack. The reduced resolver has a mixed periodic, lazy and potentially
per-decision shape.

The periodic task uses a 10-minute **fixed-delay** loop after a 90s startup delay: one invocation
finishes before the next sleep begins, so the current Python task cannot stack its own cycles.
That is only one cost surface. The same resolver also runs lazily on active-session checks, and
options (a) and (c) add per-decision callbacks/reservations unless batched. Proposed reframing, for
the operator to set numbers against:

| Original (B) | Proposed reduced (b3) |
|---|---|
| per-call p50 vs Phase A p50 × 2 | periodic **per-cycle wall-clock** plus lazy **per-invocation request overhead** |
| per-call p99 vs Phase A p99 × 3 | periodic p99 as an interval/utilisation input; per-decision p99 for every unbatched callback or reservation |
| 14-day window before thresholds settable | ⛔unchanged — a complete channel and deployment window are still owed (§3.1) |

⚠️The periodic component may be cheap when amortised over 600-second intervals, but that does not
discount request-triggered work or per-decision crossings. The threshold shape and denominator
depend on the §1.2 choice, so §1.2 must be settled before (b3) can be numeric. A future BEAM
scheduler must also state whether it is fixed-delay or fixed-rate; only the latter can stack
overlapping cycles when runtime exceeds cadence.

---

## §4 Exit criteria — rederived

⛔The go-decision §4 is explicit that criteria **2, 3 and 5 cannot stand verbatim and must be
replaced or rederived, not inherited**. Criterion 2 measures handler dispatch (excluded); the
reduced scope generates zero rows for 3 and 5 as written. Proposed replacements, numbered `R`
to keep them distinguishable from the originals:

**R1 (replaces 2 — production exposure).** The BEAM-owned sweep path has run in production for
≥30 days continuous, at the live 10-minute interval, with **both** Python entry points disabled —
the periodic background task and the lazy `is_agent_in_active_session` trigger — not shadowed.
⛔**30 days, not 21.** The original 21 was sized against continuous request traffic; at 144
scheduled cycles/day the sweeper needs calendar time, not request volume, and a task that mostly
finds nothing is not exposure. **Source:** deploy record + complete sweep/overlap channel (§3.1).

**R2 (replaces 3 — coordination incidents).** Over R1's window: complete heartbeat coverage, zero
overlaps in either writer ordering, zero guarded refusals on any explicitly permitted Python
fallback, and no new substrate-tax pattern at the sweeper's boundary. **Source:** the complete
§3.1 channel plus `audit.coordination_events` filtered to the sweeper's boundary. A positive-only
event stream or a heartbeat with the sweeper-first blind spot is insufficient; R2 must not pass by
silence.

**R3 (disposition of criterion 10's reassignment half).** ⛔The go-decision records the
reassignment-rate test as *"unpinnable in principle until reassignments actually
occur"*: two reassignments in the entire dialectic history (2026-04-19, 2026-04-30), zero
`dialectic_reviewer_reassigned` rows all-time against 4.7M events, and the clean window still not
started because **no deploy timestamp has ever been recorded** for the
`events.py::emit_reviewer_reassigned` change. Verified 2026-08-29: no such line exists in any doc.
The go-decision requires the smaller gate to *"either supply a different reassignment measure or
state that (F) rests on the resolution-rate half alone."*

**Disposition: (F) rests on the resolution-rate half alone until a genuine reviewer-behavior
measure is defined and can accrue.** Guard refusals and sweep cycles belong to R2: they measure
writer coordination, aggregate reviewer/facilitation/reap attempts, and miss successful
reassignment churn. Reusing them here would duplicate R2 while leaving criterion 10's behavioral
question unanswered. ⛔The reassignment metric is unmeasured, not disproven or retired.

**R4 (replaces 5 — boundary cost).** Per §3.2, and ⛔not numeric until §1.2 is chosen. It must
separately budget periodic cycles, lazy invocations, and unbatched per-decision crossings.

**Inherited unchanged:** 11 (behavioral parity — the sweeper's externally visible effect on
`core.dialectic_sessions` must be byte-equivalent) and 12 (test-class green: ExUnit + Python +
integration).

**Explicitly still halting, and not this gate's to lift:**
- **Criterion 10, resolution-rate half.** Last measured 2026-08-22: eligible cohort **5** against a
  floor of 30, canary-excluded, computed through `classify_outcome` / `get_outcome_breakdown()`.
  ⛔The go-decision is emphatic that it must be pinned **before** implementation and that pinning
  it afterwards measures the change against itself. ⛔It is not pinnable today and no figure in
  this document may be read as pinning it. ⛔The window slides; the 2026-08-22 numbers are quoted
  with their as-of date and must not be re-quoted without it.
- ~~**Criterion 6.**~~ ✅ **No longer halting — ruled 2026-08-29 (§6.2).** Retained as telemetry
  with its halt authority removed. `process_agent_update` p99 (795ms as of the ruling, on the axis
  struck 2026-06-24) is still measured and still reported; it can no longer stop anything. ⛔It was
  not retired — do not cite it as removed.
- **Criterion 7.** The `docs/handoffs/wave-3-mcp-sdk-spike-<date>.md` artifact exists on no ref.
  Does not fire on the merits; the artifact is owed (§6.4).

---

## §5 Stop signs, revised for this scope

Inheriting #7 (503 rate during cutover/rollback) and #10 (cross-session shared-agent invariant).
⛔Retired as out-of-subject for the reduced scope: #5 (identity middleware — no subject), #8 and
#11 (§9/§10 saga and ETS designs, both scoped out). Added:

- **#13** The BEAM sweeper writes a row that was already terminal. Halt immediately — this is the
  exact defect the port exists to remove, reproduced by the port.
- **#14** Sweep-cycle p99 consumes the operator-set interval budget, lazy invocation overhead
  exceeds its request-path budget, or a future scheduler permits cycles to overlap. Halt; (b3)
  has fired. The current Python fixed-delay loop cannot stack itself.
- **#15** Python's lazy resolver entry remains enabled after BEAM takes ownership, or a
  callback/handoff causes reviewer candidate checks to re-enter the resolver. Halt; this is (b4).
  The task-local flag is recursion suppression, not the serialization design.
- **#16** Any change to `select_reviewer`'s selection outcomes during the port. Selection was not
  authorised to move or change; a behavioural diff there is scope creep wearing a port's clothes.

---

## §6 Operator rulings — four settled 2026-08-29, two still owed

⛔**Settled by the operator on 2026-08-29**, in session, as choices stated before application
rather than method reported afterwards. §6.1, §6.2 and §6.6 carry rulings; §6.5 is deferred by
§6.6's ruling. §6.3 and §6.4 remain owed and are unchanged.

**§6.1 Does reserve-first extend to the reassignment path? — ✅ RULED: NO.** _(operator,
2026-08-29)_ **The reassignment writes get their own serialization design.** (B) reserve-first
stays specified for Invariant 5, `SYNTHESIS→RESOLVED` only, and is not stretched over a path with
different write sequences and a different critical section.

⛔**Consequence for the RFC, which must not be lost:** the RFC's write table *asserts* (B) as the
fix for the reassignment writes. That assertion is now **overruled** — it was never derived for
that path. Any future reader of `beam-wave-3-handler-dispatch.md` reaching for reserve-first there
is reaching for a mechanism this ruling declined to extend.

⛔**Consequence for §1.2:** option (c) survives, and it now carries a named design debt — the
reassignment serialization must be *specified*, not inherited. It does not collapse into (a).
⛔That specification is **not** authorised by this ruling and is not owed yet; see §6.5.

**§6.2 Criterion 6 — ✅ RULED: RETAINED, NON-HALTING.** _(operator, 2026-08-29)_ The measurement
stays; its **halt authority is removed**. `process_agent_update` p99 is recorded as telemetry with
no gate authority attached — the treatment the measurement-authority rule prescribes for a number
that informs rather than decides.

⛔It was **not** retired, and the distinction is load-bearing: retiring it would have deleted a
measurement, and the rule permits retiring an *instrument* but never on the strength of what it
reported. Keeping it non-halting removes the authority without discarding the datum. ⛔No future
reading of criterion 6 may halt Wave 3, and no future reading may cite its removal either — it is
still there, still measured.

**§6.3 The §11.9 conjunction ambiguity.** §0(E) reads as a conjunction; the v0.3 fold reads >25%
slip as a standalone halt. ⛔Explicitly reserved to the operator by the amendment itself.

**§6.4 The two missing handoff artifacts.** `wave-3-mcp-sdk-spike-<date>.md` (criterion 7) and the
`wave-3-state-ownership-redteam-<date>.md` / `-prep-` contradiction (criterion 8, original scope).
Both are non-gating for the reduced scope and both are still owed. ⛔Applying the
missing-source halt to 8 and not to 7 remains inconsistent.

**§6.5 Scope: path (1) only, or (1) and (2)? — ⏸️ DEFERRED, with a named reopen condition.**
_(operator, 2026-08-29)_ ⛔**Not answered, and deliberately not sent to council either.** §6.6's
instrument-first ruling removes this question's urgency entirely: the §3.1 instrumentation is
Python-only and requires no scope decision to build. If a **complete** window returns zero overlap
in both writer orderings, **(b1) fires and there is no port** — at which point this question
dissolves rather than gets answered. The #2011 refusal stream alone is not that window.

⛔Spending a council round on the shape of a port that may not happen is precisely the cap spend
criterion 9's apparatus exists to prevent. **Reopen condition:** the complete §3.1 instrument
reports a nonzero overlap/refusal, or the operator elects the port on other grounds. ⛔§1.1's
recommendation of path (1) stands as a recommendation only and has **not** been ratified.

⚠️The council round that *is* owed regardless is the one on this document as a whole (§8) — not
on this question in isolation.

**§6.6 Is the reduced scope worth its own cap spend? — ✅ RULED: INSTRUMENT FIRST, DECIDE ON THE
DATA.** _(operator, 2026-08-29)_ **The port is neither authorised nor declined.** What is
authorised is the §3.1 guard-refusal emitter: Python-only, no BEAM, and — because it builds nothing
in the reduced scope — **it needs no gate and does not consume the §4 build authorisation.**

⛔**This is not a deferral dressed as a decision.** It settles the question the gate could not
answer honestly: instrumentation must precede a verdict on the port. PR #2011 implemented the
positive refusal half; the post-#2008 follow-up adds the cycle denominator and early-saga skip
count. Per §3.1, the sweeper-first ordering remains blind, so the ruling's data requirement is not
yet complete and no port verdict follows from these changes.

**What it commits to:** building the instrument, and deciding afterwards on what it reports.
**What it declines to commit to:** the port, the scope (§6.5), the §1.2 design option, and the
serialization spec §6.1 authorised the *direction* of.

⛔**Both exits stay live and neither is prejudiced.** A complete window of genuine zeros fires
**(b1)** and closes the reduced scope on measured evidence. A window of real collisions makes the
ownership case on data rather than on the structural argument alone — and **(b2)** remains
available throughout. That in-place fix requires an explicit lock or shared reservation honored
by both writers; an ordinary transaction alone does not close the check/write race.

---

## §7 Sequencing

**Rewritten 2026-08-29 after the §6 rulings.** The old order led with two design questions; both
are now deferred, and the instrument moved to the front.

**Now — authorised and unblocked:**

1. **Complete the §3.1 instrument.** PR #2011 built `dialectic_write_refused` at all three refusal
   sites and stopped dropping the positive counter from periodic logs. The post-#2008 follow-up
   builds `dialectic_sweep_cycle`, including all-zero cycles, trigger source, early saga skips,
   attempts, outcomes, duration and error state; it also closes the direct/background reentrancy
   fan-out. ⛔Still owed: an observation from the saga/reservation side for the sweeper-first
   ordering. These Python-only observability changes build nothing in the reduced port scope and
   do not spend the §4 build authorisation.
2. **Deploy the complete instrument and record its timestamp and commit.** The reassignment metric
   has been stuck since 2026-06-11 for exactly this omission (§4, R3); do not repeat it. The
   window starts at *deploy*, not merge.

   ⚠️**The slot is below, empty, because that is the failure this step exists to prevent.** The
   2026-06-11 omission was not a refusal to record — it was that nobody had anywhere obvious to
   write it, so the value was never captured and the window never started. An instruction without
   a destination is how that repeats.

   | | |
   |---|---|
   | **Deployed commit** | ⛔_not yet deployed_ |
   | **Wall-clock deploy time (UTC)** | ⛔_not yet deployed_ |
   | **Denominator / coverage predicate** | ⛔_state expected periodic coverage, lazy-source treatment, and both overlap orderings when the clock starts_ |

   ⛔**No incident or cycle row predating that timestamp may be counted**, and until the row above
   is filled no window has started and none may be cited. ⛔Whoever runs the deploy fills
   this in; it is not derivable afterwards — verified 2026-08-22 that the `governance` database has
   no deploy table and no deploy-completion event, `deploy-apply.sh` is human-triggered with no
   automation, and process-start time is not a proxy because the service is `KeepAlive`-restarted
   independently of deploys.

3. **Accrue the window.** ⛔≥30 days proposed, matching R1 — a proposed prior, not a settled one.
   Neither #2011 alone nor this follow-up starts it.

**Then — the decision this gate was built to inform:**

4. **Read the window against (b1) and (b2).** Only coverage-complete zeros for both writer
   orderings close the reduced scope on measured evidence; collisions make the ownership case on
   data. ⛔Whichever it is, name which of the four states it rules out and how.
5. **Only if the port goes live:** reopen §6.5 (scope), choose the §1.2 option, and specify the
   reassignment serialization §6.1 authorised the direction of. ⛔None of these is owed before
   step 4, and none may be started on the strength of this document.

**Independent of the above, and owed regardless:**

6. **Council round on this document** (§8) — unheld, and named by the go-decision §4 alongside it.
7. **Pin criterion 10's resolution half** — needs a `resolved+failed` denominator ≥30, upstream of
   this gate and of anything in it.
8. **§6.3 and §6.4** — the conjunction ambiguity and the two missing handoff artifacts, both still
   owed and neither settled here.

⛔**Nothing in the reduced scope may be built until steps 4–5 have run and this gate is signed as
amended.** An observability prerequisite is not an implementation start, and step 1 is authorised
precisely because it is not one.

---

## §8 Council pass — owed

⛔Not held. The §4 authorisation names "its design pass and council review" alongside this
document; neither has run. This gate is not met until they have.
