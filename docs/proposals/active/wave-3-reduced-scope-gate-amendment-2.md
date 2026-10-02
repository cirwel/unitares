# Wave 3 reduced-scope gate: Amendment 2 (host-sleep heartbeat gaps)

**Status:** Draft amendment to the signed gate [`wave-3-reduced-scope-gate-v0.md`](wave-3-reduced-scope-gate-v0.md),
awaiting the operator's signature. Kept as its own record because the gate document is at the
800-line cap for active proposals. Section references (§7, §9, A5, A6, A8, A10, A12) are to the
gate document.

## Amendment 2 (2026-10-01): heartbeat gaps caused by host sleep

**Authority and timing.** Drafted 2026-10-01 for the operator's signature. Unlike Amendment 1, this
one lands **inside W_pre**, after the two gaps it classifies were observed. §9 allows that: an
amendment must land before any reading that relies on it, and step 4 has not been read. It changes
no harm-class standard, no prior in A8 other than how a gap is explained, and no window bound. It
does not change the instrument, so `instrument_version` stays `wave3-instrument-v2` and W_pre does
not restart (A10).

**Why it is needed.** The governance server runs on a laptop. When the host sleeps, the sweep loop
stops with it: no row is written, `cycle_seq` resumes without a gap, and `process_boot_id` does not
change. A5 classifies exactly that pattern as a hung loop, and A8 then counts any such span over
60 minutes as an unexplained gap, which makes the step 4 reading inconclusive (A10). Two such gaps
occurred in the first four days of W_pre:

| Last row before (UTC) | First row after (UTC) | Silence | Boot | Host evidence |
|---|---|---|---|---|
| 2026-09-28 20:27:11 | 2026-09-28 22:03:11 | 1h36m | unchanged, `cycle_seq` 12→13 | none: the host power log had rotated past it before it was examined |
| 2026-10-01 17:03:46 | 2026-10-01 19:30:26 | 2h27m | unchanged, `cycle_seq` 56→57 | clamshell sleep on battery at 17:09:22; only maintenance dark-wakes until a full wake at 19:29:46; next row 40 s later |

**B1 — a host-sleep gap is explained.** A heartbeat gap inside one boot is **host sleep**, not a hung
loop, when the host power log shows (i) a system sleep that begins after the last row and before the
next row was due, (ii) nothing but maintenance dark-wakes between that sleep and a full wake, and
(iii) the next row within one sweep period of that full wake. A host-sleep gap is explained for A8
and A10. ⛔It is still **uncovered time**: its exposure does not count and W_pre extends by it,
exactly as A8 already requires.

**B2 — the evidence is perishable, so it is captured, not recalled.** The host power log rotates
within days. A gap counts as host sleep under B1 only if the sleep and wake records spanning it were
copied out of the power log and kept outside the audit database before the reading, the same rule
A12 sets for emit failures. A gap whose records were not kept stays unexplained.

**B3 — the 2026-09-28 gap.** Its power-log records were gone before anyone looked, so B1 cannot
explain it. The operator attributes it to the same cause (lid closed, 2026-10-01). This amendment
records it as **explained by operator attestation**, a weaker class than B1: the step 4 reading
reports it separately, by name, and must say whether its result would differ had this gap been left
unexplained. ⛔No later gap may be explained by attestation; B2 applies from this amendment on.

**B4 — prevention, recorded as context.** On 2026-10-01 the operator disabled system sleep on the
host (`pmset -a disablesleep 1`), so further lid-closed gaps are not expected. That setting is a
host fact, not a gate condition: if it is reverted, B1 and B2 still decide every gap.
