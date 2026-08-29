# Wave 3 reduced-scope gate (v0) — the smaller gate owed by the 2026-08-22 signature

**Status:** ⛔ **PROPOSED — UNRATIFIED.** Nothing here is a decision. This document exists to
give the operator something concrete to accept, amend or reject.

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

| Path | Symbol | File | Signed line | Actual 2026-08-29 |
|---|---|---|---|---|
| (1) sweeper | `auto_resolve_stuck_sessions` | `src/mcp_handlers/dialectic/auto_resolve.py` | 102 | **143** |
| (1) sweeper | `_parse_timestamp` | `src/mcp_handlers/dialectic/auto_resolve.py` | 32 | **37** |
| (2) request | `check_reviewer_stuck` | `src/mcp_handlers/dialectic/handlers.py` | 211 | **249** |
| (2) request | `_apply_reviewer_reassignment` | `src/mcp_handlers/dialectic/handlers.py` | 933 | **1028** |
| (2) request | `check_timeout` | `src/dialectic_protocol.py` | 1216 | **1224** |

`_apply_reviewer_reassignment` call sites, re-verified: `handlers.py:1706`, `:2681`, `:3485`
(signed as 1589/2508/3138). The **count** of three is stable; the blast-radius finding stands.

⚠️This is recorded as a property of the artifact, not as a complaint: a scope defined by line
number decays in under a week at this repo's merge rate. **This gate defines its scope by symbol
and call-graph reachability only**, and any future restatement should do the same.

### §1.1 Proposed reduction: gate path (1) only

The go-decision §4 offers a choice — *"estimate and gate these separately, or scope only (1) plus
its dependencies."* **This document proposes: scope only (1).** The reasoning is not cost:

- Path (2)'s centrepiece `_apply_reviewer_reassignment` has **three call sites**, none of which is
  the stuck-session subject. Porting it moves a shared mutation helper used by get / takeover /
  reassign entry points, so the change's blast radius is the whole reassignment surface, not the
  stuck-session hazard. That is a *different* port wearing this one's authorisation.
- Path (1) alone is where the stated justification lives. The sweeper is the second, unguarded
  writer; path (2) already routes BEAM-first with Python fallback under
  `UNITARES_DIALECTIC_BEAM_RESOLUTION`.
- Path (1) and path (2) are **not call-closed with each other** — the go-decision verified that
  `auto_resolve_stuck_sessions` calls none of path (2)'s three symbols. Splitting them costs no
  seam that is not already there.

⛔If the operator prefers to keep (2), this gate does not cover it and a separate estimate is owed.

### §1.2 The unscoped dependency, stated as the central design question

`auto_resolve_stuck_sessions` calls `select_reviewer` (`src/mcp_handlers/dialectic/reviewer.py:323`,
~450 lines: authority scoring, cooldown, capability filtering), which carries a `contextvars`
reentrancy guard (`reviewer.py:36`) that is correct **only because everything runs in one Python
process**. The go-decision flags this "unscoped and unestimated" and it remains the single largest
risk to the cap. Three options, with a recommendation but no ruling:

| Option | Shape | Cost | Risk |
|---|---|---|---|
| **(a) Callback** | BEAM owns the sweep timer and the write; calls back into Python for each reviewer decision | smallest port | adds a boundary crossing **per decision**, straight into (B)'s budget; the reentrancy guard's process assumption survives, but BEAM is now inside its critical section |
| **(b) Carry `select_reviewer`** | port the selector too | largest port | the `contextvars` guard needs a cross-runtime redesign; ~450 lines of scoring logic is a parity surface, and parity surfaces on this RFC have a bad record |
| **(c) Split the timer from the decision** | BEAM owns *detection and serialization* (which sessions are stuck, who may write); Python keeps *selection* and calls BEAM to reserve before writing | smallest **semantic** change | requires the reserve-first question in §6.1 settled first |

⚠️**Recommended: (c), conditional on §6.1.** It is the only one that addresses the stated hazard —
ownership of the write — without either importing a per-decision boundary cost or porting a
scoring surface nobody asked to move. ⛔But (c) is *not* buildable until the operator settles
whether reserve-first extends to the reassignment path (§6.1). If it does not, (c) collapses into
(a) or needs its own serialization design.

⛔**Estimate withheld deliberately.** All three options are unestimated. An estimate that named
hours before §6.1 is settled would be an estimate of an undecided design, and the §2 cap deserves
better than a number invented to fill a cell.

---

## §2 Falsifying-evidence question, restated for this scope

> **What evidence would update us away from moving stuck-session detection and reassignment
> serialization to BEAM?**

⛔The original §0's disconfirmers do not transfer verbatim, and the reason is topological, not
cosmetic. (A) and (B) were written against a **per-request** port (handler dispatch): their
thresholds are per-call costs measured against per-call baselines. The reduced scope is a
**periodic sweeper on a 10-minute timer** (`background_tasks.py:445`,
`dialectic_auto_resolve_sweeper_task(interval_minutes=10.0)`). A per-call threshold applied to a
144-invocations-per-day background task measures nothing it was designed to measure.

### Proposed reduced disconfirmer set

Each is a **proposed prior**, stated as a choice before it is applied, per the measurement-authority
rule. None has been measured. Every measurement cell below is deliberately empty.

**(b1) The hazard is not real in production.** Over a 30-day window, `skipped_count` from the sweep
cycle is **zero** AND no session shows a reviewer or status write from the sweeper landing on a row
BEAM had already resolved. If the dual-writer collision never occurs, the ownership repair has no
subject and the correct action is to keep the #1804 write-tail guards and close.
**Measurement source:** ⛔**DOES NOT EXIST — see §3.1. This disconfirmer is unmeasurable today and
that is the gate's first prerequisite.**

**(b2) The in-place fix closes it.** A Python-side change during the implementation window makes
the sweeper's write path safe without porting — e.g. a single transaction spanning
`has_inflight_saga_async` through the status write, closing the TOCTOU window at
`auto_resolve.py:204` directly. If that lands and holds for 30 days with `skipped_count` at zero,
the port is redundant. **Measurement source:** same channel as (b1), same prerequisite.
⚠️This disconfirmer is the analogue of the original (A.2), and it is **more likely to fire here
than (A.2) ever was**: the reduced scope's hazard is a serialization bug, and serialization bugs
have in-process fixes. ⛔Naming that honestly is the point of a disconfirmer set.

**(b3) Boundary cost exceeds the coordination bought.** ⛔**Deliberately left without a numeric
threshold — see §3.2.** The original (B)'s ×2 / ×3 multipliers against lease-plane Phase A
(resident surface: p50 28ms, p99 5,146ms, n=44,535, window 2026-05-06→05-20) were derived for
per-request marshalling. They cannot be inherited. §3.2 proposes what replaces them and why the
number is the operator's to set.

**(b4) Reentrancy or selection semantics prove irreducible.** The port surfaces a decision the
selector cannot make without in-process state — the `contextvars` guard being the known candidate —
and reproducing it at the boundary reintroduces the coordination the port exists to remove.
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

### §3.1 ⛔PREREQUISITE: the dual-writer collision is currently unobservable

**This is the gate's central finding and it blocks disconfirmers (b1) and (b2).**

`auto_resolve_stuck_sessions` counts guard-refused writes in `skipped_count`
(`auto_resolve.py:186, 249, 390, 463`, returned at `:509`) — incremented exactly where #1804's
`AND status NOT IN ('resolved','failed')` predicate causes a no-write. That counter is **the direct
observation of the hazard this entire reduced scope exists to repair**: a nonzero `skipped_count`
is a sweep that tried to write a row somebody else had already finished.

It goes nowhere durable, and the reason is structural rather than a missing plumbing line.
Verified 2026-08-29:

- **The sweeper's only two emitters fire on success paths.** `auto_resolve.py` imports exactly
  `emit_reviewer_reassigned` and `emit_facilitation_needed` (`:15`) and calls them at `:274` and
  `:398` — both reached only when a write *succeeded*. ⛔**No emitter exists on the refusal path
  at any of the four `skipped_count` increments.** The refusal is not under-plumbed; it is
  unrepresented in the event vocabulary.
- The count does reach the caller as `summary["skipped"]` (`background_tasks.py:441`), and the
  sweeper's own returned `message` string spells it (`"… {skipped_count} skipped (write refused)"`,
  `auto_resolve.py:514`). ⚠️But the caller **discards `message`** and builds its own line, gated on
  `summary["failed"] or summary["reassigned"] or summary["facilitation"]` (`background_tasks.py:472`)
  — `skipped` is in neither the condition nor the text. A sweep that refuses every write logs
  nothing at all.
- No `audit.events` row, no `audit.coordination_measurements` row, no metric series. Outside
  `auto_resolve.py`, the only other `skipped_count` in `src/` is unrelated
  (`tool_registration.py:458`).

⛔**Consequence:** "the sweeper has never collided with BEAM" and "we have never been able to see a
collision" are the same observation today, and the measurement-authority rule forbids reporting
them with the same sentence. State 3 — *not recorded* — is exactly what this is.

**Proposed prerequisite PR (small, no BEAM):** add an emitter on the refusal path — a new event
alongside the existing two in `events.py`, carrying the refusing predicate, the session id and the
`source` tag the reassignment emitter already uses — and stop discarding the sweep summary's
`message`. ⛔Note this is **not** a plumbing fix to an existing signal: the event does not exist,
so it must be defined, and defining it is the prerequisite's actual content. Then start the window.
⛔Nothing in this gate may be read until that channel has produced data.

⚠️This also repairs, for free, the reassignment-metric hole that criterion 10's second half has
been stuck on since 2026-06-11 — see §4, criterion R3.

### §3.2 The (B) boundary budget must be re-derived, and its denominator changes

⛔**Do not inherit the ×2 / ×3 multipliers.** They compare a per-request payload against a
per-acquire lease ack. The reduced scope has neither shape.

The sweeper runs **every 10 minutes** (144 cycles/day) outside any request path, after a 90s
startup delay. Its boundary cost is therefore **amortised over the sweep period**, and the question
a threshold should answer is not "is a call cheap" but "does the coordination cost fit inside the
interval with headroom." Proposed reframing, for the operator to set numbers against:

| Original (B) | Proposed reduced (b3) |
|---|---|
| per-call p50 vs Phase A p50 × 2 | **per-cycle wall-clock** as a fraction of the 10-minute interval |
| per-call p99 vs Phase A p99 × 3 | **per-cycle p99** must not exceed the interval (a sweep that outruns its period stacks) |
| 14-day window before thresholds settable | ⛔unchanged — a window is still owed, and the channel does not exist (§3.1) |

⚠️**Named asymmetry, so it is not discovered later as a surprise:** on this reframing the boundary
cost is nearly free — 144 crossings a day amortised over 600-second intervals will pass almost any
threshold. **That makes (b3) a weak disconfirmer for this scope, and it should be recorded as weak
rather than dressed up as a passed gate.** The load-bearing disconfirmers here are (b1), (b2) and
(b4). ⛔An operator reading a green (b3) should read it as "the topology made this cheap," not as
evidence the port is warranted.

⚠️Option (a) in §1.2 breaks this reframing: a per-decision callback puts a crossing back on the
inner loop and the per-call form of (B) becomes the right one again. ⛔The threshold shape depends
on the §1.2 choice, so §1.2 must be settled before (b3) can be numeric.

---

## §4 Exit criteria — rederived

⛔The go-decision §4 is explicit that criteria **2, 3 and 5 cannot stand verbatim and must be
replaced or rederived, not inherited**. Criterion 2 measures handler dispatch (excluded); the
reduced scope generates zero rows for 3 and 5 as written. Proposed replacements, numbered `R`
to keep them distinguishable from the originals:

**R1 (replaces 2 — production exposure).** The BEAM-owned sweep path has run in production for
≥30 days continuous, at the live 10-minute interval, with the Python sweeper disabled — not
shadowed. ⛔**30 days, not 21.** The original 21 was sized against continuous request traffic; at
144 cycles/day the sweeper needs calendar time, not request volume, and 21 days of a task that
mostly finds nothing is not exposure. **Source:** deploy record + sweep-cycle channel (§3.1).

**R2 (replaces 3 — coordination incidents).** Over R1's window: zero rows where the BEAM sweeper
wrote a row already terminal, AND `skipped_count` on any surviving Python path is zero, AND no new
substrate-tax pattern at the sweeper's boundary. **Source:** the §3.1 channel plus
`audit.coordination_events` filtered to the sweeper's boundary.
⚠️Note this criterion is only meaningful **because** §3.1's channel exists. Without it R2 is
unfalsifiable and would pass by silence.

**R3 (replaces 10's reassignment half — a measure that can actually accrue).** ⛔The go-decision
records the reassignment-rate test as *"unpinnable in principle until reassignments actually
occur"*: two reassignments in the entire dialectic history (2026-04-19, 2026-04-30), zero
`dialectic_reviewer_reassigned` rows all-time against 4.7M events, and the clean window still not
started because **no deploy timestamp has ever been recorded** for the
`events.py::emit_reviewer_reassigned` change. Verified 2026-08-29: no such line exists in any doc.
The go-decision requires the smaller gate to *"either supply a different reassignment measure or
state that (F) rests on the resolution-rate half alone."*

**Proposed: supply a different measure — guard-refusal count (§3.1), not reassignment rate.** It is
strictly better suited to what this scope changes: the hazard is *who owns the write*, and a
guard refusal is a direct observation of two writers converging on one row, whereas a reassignment
rate is a proxy that measures dialectic activity. ⛔It may also read zero — but a zero on the
guard-refusal channel is a *measured* zero with a named producer, which is the distinction the
measurement-authority rule turns on. ⛔It does not authorise retiring the reassignment metric;
that metric is unmeasured, not disproven.

**R4 (replaces 5 — boundary cost).** Per §3.2, and ⛔not numeric until §1.2 is chosen. Recorded as
a weak disconfirmer by construction.

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
- **Criterion 6.** Halts as written because `process_agent_update` p99 improved to 795ms — on the
  axis struck 2026-06-24. Operator ruling owed (§6.2).
- **Criterion 7.** The `docs/handoffs/wave-3-mcp-sdk-spike-<date>.md` artifact exists on no ref.
  Does not fire on the merits; the artifact is owed (§6.4).

---

## §5 Stop signs, revised for this scope

Inheriting #7 (503 rate during cutover/rollback) and #10 (cross-session shared-agent invariant).
⛔Retired as out-of-subject for the reduced scope: #5 (identity middleware — no subject), #8 and
#11 (§9/§10 saga and ETS designs, both scoped out). Added:

- **#13** The BEAM sweeper writes a row that was already terminal. Halt immediately — this is the
  exact defect the port exists to remove, reproduced by the port.
- **#14** Sweep-cycle p99 exceeds the 10-minute interval, or cycles begin to stack. Halt; the
  reframed (b3) has fired in the only shape where it is strong.
- **#15** The reentrancy guard's semantics are found to be reproduced at the boundary rather than
  removed — i.e. BEAM ends up holding a lock Python used to hold in-process. Halt; this is (b4).
- **#16** Any change to `select_reviewer`'s selection outcomes during the port. Selection was not
  authorised to move or change; a behavioural diff there is scope creep wearing a port's clothes.

---

## §6 ⛔What this gate does NOT settle — operator rulings owed

Recorded openly rather than resolved by an agent. Each blocks something named.

**§6.1 Does reserve-first extend to the reassignment path?** (B) reserve-first is specified for
Invariant 5, `SYNTHESIS→RESOLVED`. The RFC's write table *asserts* it as the fix for the
reassignment writes, but as specified it does not cover them. ⛔The go-decision hands this
question to the smaller gate; the gate cannot answer it, because the answer determines which
serialization design is being gated. **Blocks:** §1.2 option (c), and therefore §3.2's threshold
shape and R4.

**§6.2 Criterion 6.** As written it halts because a struck axis improved. Ruling owed either way.

**§6.3 The §11.9 conjunction ambiguity.** §0(E) reads as a conjunction; the v0.3 fold reads >25%
slip as a standalone halt. ⛔Explicitly reserved to the operator by the amendment itself.

**§6.4 The two missing handoff artifacts.** `wave-3-mcp-sdk-spike-<date>.md` (criterion 7) and the
`wave-3-state-ownership-redteam-<date>.md` / `-prep-` contradiction (criterion 8, original scope).
Both are non-gating for the reduced scope and both are still owed. ⛔Applying the
missing-source halt to 8 and not to 7 remains inconsistent.

**§6.5 Scope: path (1) only, or (1) and (2)?** §1.1 proposes (1). Operator's call.

**§6.6 Is the reduced scope worth its own cap spend at all?** ⛔Stated plainly because a gate that
cannot ask this is decoration: (b2) is a live disconfirmer, the hazard has an in-process fix, and
§3.2 shows the boundary-cost argument is weak by topology. The honest case for proceeding is
**ownership and aliveness** — one writer, supervised, with the timer where the state is — not
latency and not cost. ⛔If the operator does not want to buy that, (b2) is the exit and it is
already named.

---

## §7 Sequencing

1. **Settle §6.1** (reserve-first scope) → unblocks §1.2.
2. **Choose §1.2 option** → fixes the threshold shape for (b3)/R4.
3. **Land the §3.1 prerequisite PR** (durable guard-refusal channel; Python-only, no BEAM) → makes
   (b1), (b2), R2 and R3 measurable at all.
4. **Accrue the window** — ⛔≥30 days proposed, matching R1; this is a proposed prior.
5. **Pin criterion 10's resolution half** — requires a `resolved+failed` denominator ≥30, which is
   upstream of this gate and of anything here.
6. **Estimate**, against a settled design, and check it against the §2 cap.
7. **Operator signature on this gate, as amended.**
8. Only then: build.

⛔Steps 1–3 are the whole of what is actionable today. ⛔Step 3 is the only one that is code, and
it is deliberately Python-only: nothing in the reduced scope may be built before this gate is
signed, and an observability prerequisite is not an implementation start.

---

## §8 Council pass — owed

⛔Not held. The §4 authorisation names "its design pass and council review" alongside this
document; neither has run. This gate is not met until they have.
