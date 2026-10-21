# Ledger triage — 2026-06-11

Read-only triage of the deferred/blocked rows in `docs/ontology/plan.md`, checking
each row's unblock trigger against today's date and against what is verifiable
without a live database. Produced from a server-only session (no live PostgreSQL),
so time-based gates are checked offline and population/corpus gates are pinned to
the exact diagnostic that must run on a live host.

**Headline:** two deferred rows (R2-Ph2, S14) crossed their *time-based* triggers
as of today, but both retain unmet *population/corpus* gates. Corpus sparsity is the
cross-cutting blocker the ledger flags repeatedly. Nothing is cleanly unblockable;
the clocks opened, the gates hold.

## Triage table

| Row | Status | Trigger | Date gate | Population/corpus gate | Verdict |
|-----|--------|---------|-----------|------------------------|---------|
| R2-Ph2 | DEFERRED | ≥28 telemetry days + 50 confirmed pairs + 10 demoted + 1 cross-role rejection | met — Phase 1 shipped 2026-05-05, 37 days elapsed | needs live `r2_phase1_telemetry.py`; R1's 2026-05-28 note (3 unique parent→successor pairs / 268 EISV-complete rows in 30d) implies confirmed_pairs far below 50 | stay deferred |
| S14 | DEFERRED | ≥4w S1-a telemetry + ≥10 R4-passing agents | met — S1-a shipped 2026-04-29, 43 days | R4-passing = substrate-earned residents (Lumen + a handful); ≥10 implausible today; no automated counter exists | stay deferred |
| Q1, Q2 | BLOCKED on R1/R2 | upstream completion | n/a | R1 consumer-wiring still deferred (#341, no R3 reader); R2-Ph2 not opened | no change |
| S9 | BLOCKED on R1 | R1 broader PATH 1/2 re-scope | n/a | R1 not fully closed | no change |
| S12 | BLOCKED | asymmetric-info channel maturity | not date-gated | needs fresh channel-geometry pass | no change |
| R1 / R5 / R6 | OPEN-actionable | already evidence-gathering posture | n/a | R1 awaits R3 reader; R5 `parent_memory_corpus_sparse`; R6 needs H7/H8/gateway/Discord/cron/Dispatch evidence | no change from triage |

## Thresholds (pinned from code)

R2-Ph2 gate, from `src/identity/r2_phase1_telemetry.py`:

- `DEFAULT_PHASE1_START = 2026-05-05`
- `DEFAULT_MIN_TELEMETRY_DAYS = 28` — met (37 days as of 2026-06-11)
- `DEFAULT_MIN_CONFIRMED_PAIRS = 50` — unverified offline
- `DEFAULT_MIN_DEMOTED_PAIRS = 10` — unverified offline
- `DEFAULT_MIN_CROSS_ROLE_REJECTIONS = 1` — unverified offline

S14 gate, from `plan.md` status board: `≥4w S1-a telemetry (S1-a shipped 2026-04-29)
+ ≥10 R4-passing agents`. The R4-passing condition is the substrate-earned-identity
pattern (`docs/ontology/identity.md` appendix); there is no diagnostic that counts
qualifying agents, so this is a manual population check.

## Live-verification commands (run on a host with the governance DB up)

```bash
# R2-Ph2 — confirmed/demoted/cross-role counts vs. thresholds
python3 scripts/diagnostics/r2_phase1_telemetry.py --json

# S14 — manual count of R4-passing (substrate-earned) active identities; no automated counter
```

## Recommendation

Keep R2-Ph2 and S14 deferred. Re-run the R2 telemetry diagnostic on a live host to
turn the "almost certainly below threshold" inference into a recorded count; if the
corpus is still sparse, the deferral stands on evidence rather than on date. Do not
flip any blocked row — every upstream dependency (R1 consumer wiring, R2 Phase 2,
asymmetric-info channel maturity) is still open.
