# Consult constraint records

Dated evaluation records for the `consult` critique wrapper's format-following
behavior on a local model. They were moved here from `docs/integration/` on
2026-10-20; their text and data are unchanged.

| Record | What it shows |
|---|---|
| [`consult-constraint-probe-2026-10-13.md`](consult-constraint-probe-2026-10-13.md) | Six-call synthetic probe: current and candidate wrappers both met all three tested format constraints; the reported overlong critique was not reproduced |
| [`consult-constraint-replay-2026-10-14.md`](consult-constraint-replay-2026-10-14.md) | Exact-brief replay of the reported request: reproduced the verbosity failure with the old wrapper and evaluated the revised one |
| [`consult-constraint-replay-2026-10-14.json`](consult-constraint-replay-2026-10-14.json) | Retained local replay data for the replay record |

## How to read them

Both records are format-following evidence for one local model and a small set
of briefs. They are not an estimate of a general failure rate and make no claim
about advisory quality. The probe is superseded on the reported failure by the
replay: its synthetic briefs began with `Proposal:` and did not expose the
case where an imperative brief is read as material to critique. Its six-call
result still stands as evidence about those synthetic briefs.

The three replayed samples of the original request share one brief and one
model, so they are not independent coverage of the wider consultation
workload. Other purposes and cloud hosts were checked deterministically for
prompt routing only. The replay also records a separate Claude consultation that
ended with `adapter_status="malformed"`; its cause remains unresolved.

The wrapper itself lives in `src/mcp_handlers/support/consultation.py`.
