# KG matched-corpus feasibility v1

**Status:** prospective proposal; no model-assisted study enrolled or authorized

**Scope:** repair the isolated infrastructure probe, then prepare a descriptive
comparison of native KG PostgreSQL FTS and a lexical substitute over the same
actual records. This proposal does not reopen the stopped v0 enrollment.

## Preserve the earlier result

The [v0 protocol](../archive/kg-agent-adoption-pilot-v0.md), frozen tasks,
source map, captured probe and receipts remain unchanged. Four synthetic logical
sources were derived from one live record, the live root missed five of six
top-five queries, immediate audit validation failed, and a later host check-in
contaminated the canary's calibration partition. These remain adverse evidence.

The separate 90-step code-reading QA pack is not a KG task corpus. It supplies
neither live source parity nor scientific enrollment authority.

## Infrastructure repair

`scripts/eval/kg_canary_runtime.py` seals an exact interpreter, bootstrap,
Python/native module origins, dependency metadata, selected database settings,
and the original canary inputs. Its preparation mode disables network access.
The child starts with `-I -S -B`, installs guards before application imports,
compiles verified Python source rather than accepting an existing bytecode
cache, and does not process site startup hooks.

The selected DB assignments are committed with a private random-key HMAC.
The approval covers that commitment and the non-password target metadata as
part of the runtime digest. The child validates the exact manifest bytes against
the parent's approved digest and reserved attempt before enabling networking.
It receives no model credentials or interactive host session/slot bindings.

An immutable, directory-synced attempt precedes process creation. Final outcomes
are separate atomic publications; an interrupt, timeout or lost parent never
authorizes a replay. Logs are bounded and retained. Parent interruption kills
and reaps its child group; a child watchdog terminates on parent loss/deadline.
These are safeguards for a trusted, reviewed Python workload, not an OS sandbox
against compromised native extensions or the machine owner.

A fresh **infrastructure-only** invocation still requires an independent,
on-record review of the exact prepared runtime and explicit operator authority.
Its bounds remain two details pages, six unchanged FTS/OR/top-five searches,
at most one explicit neutral audit append, no retry, no KG writes and no model
calls by the client. The output remains HOLD, with parity and scored-run claims
false. Ordinary server identity/tool-usage telemetry is not claimed absent.

The previous private launcher remains withdrawn. Its rejected execution is not
relabelled as approved by an implementation review or this proposal.

After child exit, an independently authorized read-only observation can report
measured-state, outcome and adoption-event counts for the canary identity over
the actual observed interval. A finite zero must be reported as an observation,
not proof of perpetual exclusion. No new quiet-period threshold is established
here.

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
not establish a snapshot. A connection-bound acquisition adapter must be tested
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
stored search vector. Decode JSON, serialize records in ID order with sorted
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
   Retain IDs, ranks, content hashes, latency, errors and missingness.

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
