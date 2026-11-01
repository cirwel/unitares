# KG matched-corpus feasibility v1

**Status:** prospective proposal; no model-assisted study enrolled or authorized

**Scope:** prepare a descriptive comparison of native KG PostgreSQL FTS and a lexical substitute over the same
actual records. This proposal does not reopen the stopped v0 enrollment.

## Preserve the earlier result

The [v0 protocol](../archive/kg-agent-adoption-pilot-v0.md), frozen tasks,
source map, captured probe and receipts remain unchanged. Four synthetic logical
sources were derived from one live record, the live root missed five of six
top-five queries, immediate audit validation failed, and a later host check-in
contaminated the canary's calibration partition. These remain adverse evidence.

The separate 90-step code-reading QA pack is not a KG task corpus. It supplies
neither live source parity nor scientific enrollment authority.

## Withdrawn live canary; offline inspection only

The attempted live-write execution design was rejected in governed review
`afb1859ca456187a`. No invocation, fresh canary identity or audit append occurred.
The exact prepared runtime digest
`48b9e5d210e50d608039e4b6693619831410a03af11c445a5047de6aed97ca73`
is withdrawn permanently. Its rejection and withdrawal are not execution approval.

`scripts/eval/kg_canary_runtime.py` now exposes only `seal` and `prepare`.
Both the internal child and launcher reject `run` before doing work; its audit
guard denies all socket events. It retains offline source/input validation,
private DB-target commitments, source-bound imports and supervised process
cleanup. It cannot perform a canary invocation. Earlier evidence remains intact.

## Read-only feasibility implementation

`scripts/eval/kg_snapshot_feasibility.py` has separate `plan` and `execute`
commands. Planning makes no database connection. Each capture or evaluation plan
binds its exact code/dependency files, interpreter, selected DB settings (private
HMAC), output directory, global one-use reservation, explicit bounds and queries.
Execution requires an independently reviewed approval binding the exact plan
SHA-256 and operator authorization reference. The reservation is outside the
copyable artifact directory and is durably created before any connection.

The runner opens one native asyncpg connection with read-only defaults, UTC,
UTF-8, fixed search path, finite statement/lock/idle timeouts and a 45-second total
timeout. It checks a physical table with no row-level filtering, enters a
read-only repeatable-read transaction, and uses a connection-bound adapter for
the existing FTS method. It never initializes the normal application, a pool,
MCP, identity, audit infrastructure or model client. No intentional application,
data or audit writes occur; ordinary PostgreSQL logs/statistics may change.
Connection loss, drift or a query failure terminates the operation; there is no
retry or reconnect path. Partial query receipts remain private and are not
reported as a completed comparison.

## Prospective corpus boundary

The first proposed scope is the existing `agent-adoption` tag, excluding archived
and cold records. Selection is by this recorded topic rule before retrieval
results are examined. Capture **every eligible record, including distractors**;
do not export only the records that support an answer. Proposed preparation
bounds are 200 records and four MiB of canonical record bytes. Exceeding either
bound aborts instead of sampling or truncating.

Neither capture nor evaluation is authorized by this proposal. Each requires a
separately recorded, bounded, read-only production operation under operator
authority; evaluation permission must bind its frozen manifest, exact query
count, DB target and transaction lifetime.
Keep complete records, actual discovery IDs, text, tags, status and provenance
privately. Publish only bounded metadata and digests. No private record content
is sent to an external model under this feasibility proposal.

Corpus equality must cover the entire search universe before ranking. The
current public search schema has no exact discovery-ID allowlist. Searching
global top-K and filtering it to an export afterwards is not an equal-corpus
comparison.

The smallest proposed implementation uses the existing PostgreSQL backend's
`kg_full_text_search` method on a held **read-only, repeatable-read transaction**.
Enumeration and every FTS query must use that exact connection/transaction;
an unrelated transaction around a method that acquires another connection does
not establish a snapshot. The connection-bound acquisition adapter is tested
with an intervening concurrent commit in a disposable database before evaluation.
Its existing tag/status predicates define the complete eligible universe.

The lexical arm is specifically `scripts.eval.kg_agent_adoption.lexical_search`
(BM25-style, `k1=1.5`, `b=0.75`), not the substring-count SQLite helper. Bind
its source-file digest at enrollment. Project each full canonical record to
`source_id=str(id)`, `title=summary`, and `text=details + "\n" + " ".join(tags)`.
No record may be dropped because a projection is inconvenient. Invalid/empty
projections abort preparation. Both arms retain the identical complete record
documents; tokenization, path decomposition and field weighting are algorithm
differences and must be described explicitly.

The proposed `canonical_kg_universe_v1` codec uses SQL predicate
`tags && ARRAY['agent-adoption']::text[] AND status IS DISTINCT FROM 'archived'
AND status IS DISTINCT FROM 'cold'`, ordered by the actual unique discovery ID.
Capture every full SQL row using `to_jsonb(row)`, including provenance and the
stored search vector. Decode JSON with exact decimal numbers (without float rounding), serialize records in ID order with sorted
keys, UTF-8, compact separators and one trailing newline per record; reject
nonfinite numbers and duplicate/missing IDs. Bind schema, predicate, count,
byte count and SHA-256. Empty or over-budget universes abort without sampling.

A two-stage freeze separates task preparation from evaluative retrieval:

1. Capture and hash the complete approved universe in a read-only transaction.
2. Prepare one substantive three-step chain requiring multiple actual records.
   Read records for curation, without testing its queries against either backend.
3. Freeze questions, objective keys, material source IDs, all query strings,
   corpus bytes, ranking/operator/K, normalization, order and failure handling.
4. Open the evaluation transaction, recapture the eligible universe, and require
   its complete canonical fingerprint to equal the frozen capture. If it differs,
   stop this enrollment. Do not silently refresh or substitute records.
5. Run each frozen query once per backend using the held transaction for FTS.
   Retain IDs, ranks, frozen corpus content hashes, latency and missingness.
   Native ordering uses rank and creation time without a final ID tie-breaker;
   retain returned order and disclose cutoff ties rather than changing it.

This measures the actual KG **database retrieval component** over actual records
at a defined snapshot. It does not measure the full MCP service, graph expansion,
reranking, live corpus evolution, agent utility, adoption or generality.
Before/after hashes alone are not described as transactional isolation; the
transaction supplies that boundary.

Retrieval misses remain visible diagnostic results. They do not turn the old
v0 preflight into a pass, establish a benefit, or authorize a model phase.
No replacement questions, query tuning, tag changes or repeated attempts are
allowed within a frozen enrollment.

## Later model-assisted enrollment

There is no scored execution path or model-assisted enrollment in this slice.
Before one is added, freeze the exact model/provider, complete chains, identical
neutral tool interfaces, unavailable-memory control, assignment/analysis seeds,
context and identity isolation, audit envelope, decomposed costs, private raw
outputs and total authorized spend.

The operator retains deciding effect floors, uncertainty/power standards,
utility weights, exclusions and preregistered stop rules. Existing choices may
be proposed explicitly, but are not silently inherited as permission for a new
experiment. The earlier five-dollar API pilot ceiling is not increased here.

## Promotion boundary

Infrastructure repair, corpus integrity, retrieval observations and behavioral
benefit are separate records. No success in an earlier layer promotes a later
one automatically. Live KG writes, import/restore tools, autonomous alerts,
measurement-to-actuation and async orchestration remain outside scope.
