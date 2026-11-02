# Matched-corpus retrieval feasibility v1

**Observed:** 2026-11-02. **Status:** completed descriptive database-component check.

The native PostgreSQL FTS component and the existing lexical BM25 substitute
each returned the two declared material sources for every frozen question:
six successful source checks per arm across one three-step chain. Rankings
differed. These are retrieval observations; behavioral evidence, efficacy,
adoption, scored-run authorization and v0-rescue claims remain false.

## Execution boundary

The operator approved the prepared dedicated SELECT-only role on the existing
database. Its direct login, read-only default, absence of administrator powers,
role memberships, table ownership, table/column write privileges and maintenance
privileges were verified. The application configuration was unchanged.

T0 capture and T1 evaluation received separate independent execution approvals,
after independent local inspection of the full private manifests. All 27,205
mapped runtime files matched their hashes; the runtime maps were identical.
Capture inspected no task queries. The chain, three query strings, objective
keys and material source IDs were then frozen together, before either search
function was run.

Both operations used one native connection with read-only repeatable-read
enforcement. The same held connection supplied complete enumeration and native
FTS. T1's full canonical corpus and lexical projection matched the frozen T0
fingerprints before retrieval. Each query ran once per arm, with OR, K=5 and a
fixed native-first order. Each operation consumed its external one-use
reservation. Both completed without retries, reconnects or query tuning.

Bounds were 200 accepted records, four MiB of SQL JSON and canonical bytes,
10-second statement and two-second lock timeouts, and a 45-second timeout around
connection/database operations. Preflight hashing was outside that last timer.
The driver retrieved canonical governance approval before launch; the runtime
made no MCP or model calls, created no identity, appended no audit event and
performed no intentional KG/application-data writes. Ordinary PostgreSQL
connection logs and statistics may change.

## Corpus and observations

The complete eligible universe contained **six actual records, 24,532 canonical
bytes**, selected by the frozen `agent-adoption` tag predicate with archived and
cold rows excluded. Each task declared two material records and retained the
other four documents in both retrieval arms. There were five distinct material
records across the six checks; a record used in two steps counts twice here.

Each rank pair follows the private frozen task's material-source order.
An absent material source would be recorded as missing; none was missing.

| Step | Native material ranks | BM25 material ranks | Native / BM25 result counts | Shared result IDs |
|---|---|---|---|---|
| 1 | 1, 3 | 2, 1 | 5 / 5 | 5 |
| 2 | 1, 2 | 2, 1 | 4 / 4 | 4 |
| 3 | 3, 2 | 3, 1 | 5 / 4 | 4 |

Native call latencies were 6.479, 0.963 and 2.572 ms; BM25 call latencies were
0.322, 0.276 and 0.291 ms. Fixed order, connection overhead, import timing and
uncontrolled caches limit their interpretation to instrumentation observations.
The native third result list contained one equal-score pair. Native rank and
creation-time ordering was retained; the production method has no final ID
tie-breaker. No extra query was made to investigate cutoff ties.

The native method uses the stored English search vector, field weighting and
path decomposition. BM25 uses the frozen summary/details/tags projection with
`k1=1.5`, `b=0.75`. The universe is identical; these algorithms differ in
tokenization, weighting and ordering.

## Evidence commitments

Raw records, query text, objective keys, credentials, private configuration,
retrieval IDs and individual query receipts remain local. The shared record is
limited to these bounded observations and commitments.

| Artifact | SHA-256 |
|---|---|
| T0 capture plan | `9686d8415daa28f22bec2f45d877860cd08f3cb22bcdf5b4a603d808a3807853` |
| Complete T0/T1 canonical corpus | `2a356aed97d7c33b2cdf2a3c36825043b2d314beb26b03db5fa40aa3b51293c2` |
| Frozen task file | `bf65dab226ce9aca95d934e967bb5b8853f934addc7c78805fcc3e0fbe0eacfc` |
| T1 evaluation plan | `5afb783be60527ffd7493b7aad2c2589e0eb5a4885082ac1115e603a0824b959` |
| Combined T1 result file | `b4e36cff96591e61c2e58c4d35e439a10d9c37854ce91a71fdf5a4c38aedee20` |

Governed approvals: capture `1b44cb28a44237dc`; evaluation
`65109dbd64f267e0`. Both bind the exact plan and operation ID; neither grants a
future invocation. No paid model request was made by either operation.

## Interpretation and preserved evidence

This verifies that the prepared database-component comparison could run over
one unchanged, complete, actual record universe. Six closely related records
with K=5 and three curator-authored questions supply a small sanity check.
Material-source presence does not score answer quality, causal utility,
adoption, generality or the full MCP service. Objective keys were frozen and
source-bound; they have no independent gold-standard validation claim.

The [stopped v0 protocol](../../proposals/archive/kg-agent-adoption-pilot-v0.md),
original inputs, root misses, derived-source mismatch, audit-validation failure
and later calibration contamination remain unchanged adverse evidence. This
run used a separate corpus/task enrollment and does not repair that result.

The [prospective protocol](../../proposals/active/kg-agent-adoption-matched-corpus-v1.md)
still requires a separate model-assisted enrollment and operator decisions for
scientific thresholds, utility weights and stop rules before a scored study.
Earlier temporary PostgreSQL test instances violated the repository policy;
all were stopped and removed, and the starting fixture was deleted. Production
operations used the existing instance throughout.
