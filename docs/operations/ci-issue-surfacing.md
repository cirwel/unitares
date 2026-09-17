# CI Issue Surfacing (experiment)

An experiment in wiring UNITARES's "issue surfacing" instinct into GitHub CI:
run deterministic collectors on a PR / on demand, and turn each *new* finding
into a deduped GitHub issue.

This is the CI-side counterpart to the in-server surfacing agents
(`agents/watcher`, `agents/vigil`). Those need a live governance server and an
LLM detector; a stock GitHub runner has neither. So the CI bridge uses
collectors that run on a vanilla `ubuntu-latest` with no Postgres, no Ollama,
and no secrets beyond `GITHUB_TOKEN`.

This document covers two directions, built as separate tools. Everything
through *Why surface-only (for now)* is the repo-to-GitHub half, which files
issues. The final section is the GitHub-to-record half: an operator-side
producer that reads those issues into the governance record.

## Pieces

| Piece | Path | Role |
| --- | --- | --- |
| Bridge | `scripts/ci/surface_findings.py` | Runs collectors, emits a normalized, fingerprinted findings JSON. Never touches GitHub. |
| Workflow | `.github/workflows/surface-findings.yml` | Runs the bridge; turns new fingerprints into deduped issues (dispatch) or a job-summary table (PR). |
| Tests | `tests/test_surface_findings.py` | Pin fingerprint parity, severity mapping, dedup, collector parsing. |

## Collectors

- **`ruff`** (default) — `ruff check --output-format=json` lint diagnostics.
  Diff-scoped on PRs. The diff-relevant code signal.
- **`doctor`** (opt-in) — `scripts/dev/unitares_doctor.py --json` fail/warn
  checks. Meaningful only on a host that *has* the governance stack; on a bare
  runner it would surface "no Postgres" noise, so it is **not** a default.
  Enable it (`--collectors ruff doctor`) on a job that provisions Postgres.
- **`watcher`** (opt-in, LLM) — defers to `agents.watcher`. Probes
  `WATCHER_OLLAMA_URL` first and skips cleanly when the endpoint is absent (the
  normal CI case). Off unless `--enable-watcher` is passed.

## Dedup contract

Each finding gets a 16-hex `fingerprint = sha256(source|rule|file|line)[:16]`,
byte-identical to `agents.common.findings.compute_fingerprint` (a parity test
pins this). Every opened issue carries a hidden
`<!-- finding-fingerprint: X -->` marker and the `ci-finding` label. Before
opening an issue the workflow reads open `ci-finding` issues, harvests their
markers, and skips any fingerprint already on the board — so re-runs never
duplicate an issue for a still-present finding. Close the issue when fixed; a
still-present finding will not re-open while the issue stays open.

## Triggers (deliberately different blast radius)

- **`workflow_dispatch`** — the experiment button. Opens/dedups GitHub issues.
  Run it from the Actions tab; optionally set the `collectors` input.
- **`pull_request`** — runs the bridge against the PR's changed `.py` files and
  writes a **job-summary table only**. No issue spam on every push.

Findings are advisory: a finding never red-Xes the job. The bridge supports
`--fail-on <severity>` for callers that want it as a gate, but the workflow
does not pass it.

## Try it locally

```bash
python3 scripts/ci/surface_findings.py                 # ruff over the repo
python3 scripts/ci/surface_findings.py --paths src     # scope to a subtree
python3 scripts/ci/surface_findings.py --collectors ruff doctor   # add host checks
python3 scripts/ci/surface_findings.py --output findings.json     # same JSON the workflow consumes
```

To watch it actually open an issue: push a branch that introduces a lint
regression (e.g. an unused import), then run the workflow via `workflow_dispatch`.

## Why surface-only (for now)

This experiment is the **surface** half of a surface→fix→land relay. The **fix**
half (dispatching an agent to open a fix PR) needs `anthropics/claude-code-action`
plus an `ANTHROPIC_API_KEY` repo secret, and is intentionally out of scope here.
The deduped `ci-finding` issues are the hand-off point a fix loop would later
consume. The **land** half — branch protection + operator-armed merge-when-green,
with the agent stopping at ready-for-review — is planned in
[`merge-automation-plan.md`](./merge-automation-plan.md).

## Carrying findings the other way: GitHub to the record

Everything above runs **repo to GitHub**: a collector or a guard computes a
finding on a runner, and a new fingerprint becomes a deduped `ci-finding`
issue. `scripts/dev/ingest_ci_findings.py` runs the other direction, **GitHub
to the governance record**: it reads those issues and stores each one the
knowledge graph does not already hold. The two directions are separate tools
that share exactly one thing, the dedup contract above: the `ci-finding` label
and the hidden `<!-- finding-fingerprint: X -->` marker.

Before this producer, nothing carried a finding into the record at all. The CI
side files GitHub issues, `scripts/ci/surface_findings.py` emits the findings
JSON the workflow files them from, and the in-server surfacing agents
(`agents/watcher`, `agents/vigil`) read governance anomalies rather than GitHub
issues. A finding could therefore sit fully visible on the issue board and be
entirely absent from the record a later agent searches. The previous section
names the deduped `ci-finding` issues as the hand-off point a fix loop would
consume; this producer consumes that same hand-off point for the record rather
than for a fix.

| Piece | Path | Role |
| --- | --- | --- |
| Producer | `scripts/dev/ingest_ci_findings.py` | Reads `ci-finding` issues through `gh`, searches the graph for each fingerprint, stores the ones that are missing. Stdlib only. |
| Tests | `tests/test_ingest_ci_findings.py` | Run the producer as a subprocess against a stub `gh` and a loopback HTTP stand-in for the REST endpoint; assert on the request bodies, not only the exit code. |

### What it repairs

Incident #2168 was reconstructed twice, once from git, PR and CI artifacts and
once from UNITARES records alone. The comparison is
`docs/evaluations/accountability-journey/capture-stage-2168-v0.md`: the
artifacts arm recovered the causal chain, the records arm recovered no positive
fact about the incident, and the miss decomposed into three separately
repairable causes. The first of them is the absence of a CI-to-kernel producer.
This script is that one lever and nothing more. The other two named causes, a
portable incident bundle and successor retrieval, are untouched here.

It adds no MCP tool, no agent, no workflow, no schema change, and nothing on
the server side. It reads two surfaces, the GitHub issue list through `gh` and
the knowledge graph through the `knowledge` tool, and writes one,
`knowledge(action='store')`.

### Run it

```bash
python3 scripts/dev/ingest_ci_findings.py                                  # dry run: the default
python3 scripts/dev/ingest_ci_findings.py --apply                          # write the missing findings
python3 scripts/dev/ingest_ci_findings.py --repo owner/name --state all --limit 400
```

Dry run is the default, and `--dry-run` says so out loud; the two write modes
are mutually exclusive, so `--apply --dry-run` is refused. A dry run still
searches, so its verdicts are the real verdicts and only the write is withheld.

Other flags: `--repo owner/name` (validated before any call is made),
`--state open|all` (default `open`), `--limit N` (default 200, matching
`merge_loss_common.find_open_finding` so this producer's view of the board is
never narrower than the guards' own), `--rest-url URL`,
`--timeout SECONDS` (default 30).

One verdict line per finding, not per issue, because one issue can carry more
than one marker. Then a tally line (`tally: stored=N would-store=N
already-recorded=N skipped-no-marker=N skipped-closed=N`):

| Verdict | Meaning |
| --- | --- |
| `storing` | Written. The line names whether it is `new` or a `followup` on an earlier record of the same fingerprint, which channel it came from, and the returned `discovery_id`. |
| `would-store` | Missing from the graph; a dry run stopped short of writing it. Carries the same `new`/`followup` and channel detail. |
| `already-recorded` | The graph already carries this fingerprint for this issue, so nothing was written. |
| `skipped-no-marker` | The issue has the label but no fingerprint marker on a line of its body or any comment, so it has no dedup key. Counted and named, never silently dropped. |
| `skipped-closed` | The issue is not open, so it is reported and not stored. See Limits. |

Exit status is 0 when the run completed and examined every issue it read. It
is 1 when the run did not complete, and in that case the tally line still
prints before the error, so what had landed by then is stated rather than
left to be guessed. A read that came back at the `--limit` ceiling also exits
1, for the reason under *It fails loud* below.

### Environment

| Variable | Default | Role, and where the name comes from |
| --- | --- | --- |
| `GITHUB_REPOSITORY` | `cirwel/unitares` | Default for `--repo`, falling back to that literal. The literal is the same one `scripts/dev/stranded_work_audit.py` and `scripts/dev/stuck_draft_audit.py` carry as a plain argparse default; neither of those reads an environment variable for it. |
| `GOV_REST_URL` | `http://127.0.0.1:8767/v1/tools/call` | Default for `--rest-url`. The variable is the one `agents/common/config.py` already reads for that endpoint. |
| `UNITARES_MCP_BEARER_TOKEN` | unset | Sent as an `Authorization: Bearer` header, and only when non-empty, which is the contract `agents/sdk/src/unitares_sdk/sync_client.py` follows. |

Those three are the whole environment surface, and each has a flag override
except the bearer token. `GITHUB_REPOSITORY` and `GOV_REST_URL` reach no row in
`docs/FLAGS.md`, for two reasons that both hold: that catalog is generated from
the shipped Python roots (`config/`, `src/`, `agents/`, `governance_core/`) and
byte-compared in CI, so no render can reach a script under `scripts/`, and it
indexes only `UNITARES_*` and `GOVERNANCE_*` names plus a curated allowlist, so
an unprefixed name would not be indexed even from a scanned root.
`UNITARES_MCP_BEARER_TOKEN` does already have a row there, generated from
`src/gateway/client.py` and others; this producer follows that contract
unchanged rather than introducing a variable. The catalog is not editable by
hand, so this table is the reference for all three.

### What counts as one finding, and what counts as already recorded

A second run over the same issue board writes nothing. The producer searches
before every write, so the guarantee comes from the record itself rather than
from local state, and it holds across machines and after a `data/` wipe. Five
details decide what that search means:

- **The key is the pair, fingerprint and issue number.** Both halves go into
  the record twice over: verbatim, on a `finding-fingerprint:` line and a
  `github-issue: #N` line inside `details`, and normalized, as the record's
  first tag and an `issue-<n>` tag. Keying on the fingerprint alone loses
  recurrences: the guards refile a fingerprint as a new issue once the previous
  one is closed, and a fingerprint-only key reported that second issue as
  already recorded and wrote nothing.
- **A recurrence is linked, not duplicated.** When the graph holds the
  fingerprint under a different issue, the new record is stored with
  `response_to: {discovery_id, response_type: 'follow_up'}` pointing at it, so
  the escalation reads as one thread rather than two unrelated notes.
- **Only the labelled forms of a fingerprint count as a match.** A row matches
  on the exact fingerprint tag, or on `finding-fingerprint: <slug>` or the HTML
  marker in a text field. A bare occurrence of the slug does not. Three of the
  four live emitters use a constant, word-shaped fingerprint
  (`stranded-work-audit`, `sdk-release-sync`, `server-release-sync`), so a bare
  match let any pre-existing note whose prose contained that phrase suppress
  that guard's findings for good, and the full-text probe on those tokens is
  what retrieves exactly such notes. Slug boundaries still apply, so a longer
  fingerprint that merely starts with a shorter one
  (`merge-content-check pr-1610` against `merge-content-check pr-16`) does not
  suppress the shorter one's write.
- **Markers are read from comments as well as the body, and only from a line
  start outside a code fence.** `merge_loss_common.file_or_comment_finding`
  comments the marker plus the new body onto an existing open issue, and
  `stranded-work.yml` does the same every week, so a recurrence, and sometimes
  a different guard's finding entirely, lands in a comment while the issue body
  stays frozen at the first detection. Issue #2168 is the recorded case: its
  body carries the `orphan-push-guard` marker and its only comment carries the
  `stranded-work-audit` one. The line-start and fence rules are the other side
  of the same exactness: every emitter writes the marker as the first line of
  what it posts, so a marker quoted in prose or inside a fence is a quotation,
  and treating one as a finding let an unrelated issue take over a real
  finding's key.
- **The text probe runs first; the tag filter is the backstop.** Only when the
  text probe returns no row carrying the fingerprint for this issue does a
  second probe filter on the exact fingerprint tag. These slugs contain `/` and
  `:`, which the search tokenizer mangles; that is the same reason
  `scripts/ci/merge_loss_common.py` matches markers against issue bodies in
  process instead of through GitHub search. Without the backstop a mangled
  query would duplicate every finding on every run.

An issue with no marker anywhere is the one case with no dedup key, and it is
never stored.

### It fails loud, where the guards fail open

The CI guards fail open on purpose: a broken guard must not block delivery.
This producer has no delivery to block, and the failure it exists to remove is
precisely a run that reports success while writing nothing. So it refuses to
report a store it cannot prove. Each of these ends the run with exit 1 and the
underlying message on stderr:

- a missing `gh`, a `gh` error, a `gh` timeout, or non-JSON output from it;
- a transport error or a request timeout;
- an outer REST envelope that is not a success;
- a tool-level failure carried *inside* a successful envelope. This is the
  common case, not an edge one: `_build_http_tool_response` wraps any handler
  result as `{"result": <parsed>, "success": true}`, and `error_response`
  puts `{"success": false, "error": ...}` in that parsed body, so the outer
  key says only that a handler ran;
- the typed identity refusal, which is the one success-shaped payload that is
  not a success. It carries no `error` key and no `success: false`, so it is
  matched on its `rollout_flag` marker and its `status`;
- a store whose reply carries no `discovery_id`;
- a search whose result shape holds no recognizable rows key, because reading
  that as a genuine zero would duplicate every finding on every run;
- a read that came back at the `--limit` ceiling. `gh` truncates there without
  saying so, so at the ceiling the producer cannot tell a full board from a
  clipped one, and a clean exit would be instrumentation failing toward
  "healthy" — the posture `merge_loss_common.py` names as forbidden for the
  guards. The tally still prints first.

An HTTP 503 is the one retried case: once, after the delay the server asks for
in a `Retry-After` header or in a typed-unavailable body, capped locally at 30
seconds, and then it fails. A non-503 HTTP error carries the `error` string
from the response body, since that is where `/v1/tools/call` puts the reason
for a 400 or a 500.

It also runs pull-side rather than from CI for a contract reason. The guard
workflows are `GITHUB_TOKEN`-only, which `tests/test_merge_loss_guards.py`
asserts, and writing to the governance server from a runner would need both a
reachable endpoint and a bearer credential in the workflow environment. Running
on the operator's machine keeps that contract intact and needs no new secret.

### Scheduling it

Nothing in this repository fires it. A daily or weekly run is enough, because
the issue board is the queue: findings persist as open issues, so a missed run
loses nothing and the next run picks up whatever is still open. A sensible
cadence is one dry run to read the board, then `--apply` on the same schedule
as the other operator-side audits, from whatever scheduler the machine already
uses. It belongs on a machine that both has an authenticated `gh` and can reach
the governance server, which is why it sits beside those audits rather than in
CI. No scheduler unit ships with it: new platform surface is frozen, and the
wiring decision is the operator's.

### Limits

- It is not the portable incident bundle, and it is not successor retrieval.
  Those are separate levers from the same comparison.
- It runs only when an operator or a scheduler runs it.
- End-to-end verification against a live governance server has not been run.
  The request and response shapes are reproduced by reading
  `agents/sdk/src/unitares_sdk/sync_client.py`, the knowledge handler, and the
  refusal builders in `src/http_routes/tools.py` and
  `src/mcp_handlers/identity_bootstrap.py`, and are covered behaviorally
  against a stub `gh` and a loopback HTTP server, not observed against a
  running server. A first live run should be a dry run, to confirm the search
  probe shape before anything is written.
- The `gh` read asks for `comments` alongside the other issue fields, which
  has not been exercised against a real `gh` here either. A `gh` too old to
  serve that field fails the read loudly with its own list of valid fields,
  rather than returning issues with the comment channel silently missing. How
  many comments `gh` returns per issue is its own contract, so an issue with a
  very long comment history may carry a marker the read does not reach.
- It does not onboard and passes no `client_session_id`, so it binds by
  whatever the transport gives it. On a deployment whose knowledge write gate
  is armed, an unbound caller's reads pass and its writes are refused, and the
  refusal arrives as a structured success-shaped payload rather than an error.
  The producer checks for that shape explicitly and exits non-zero carrying the
  refusal's own hint and next step. So on such a deployment a refusal is the
  expected outcome, not a surprise: it is a configuration question about how
  this caller should bind, and not something the producer retries around.
- A recurrence of the same fingerprint commented onto the *same* issue is
  reported as already recorded, and the newer comment text is not added to the
  record. The record then holds the first detection plus the `issue-updated`
  timestamp and the issue URL, which is where the current state lives. Storing
  on a newer timestamp instead would grow without bound on the weekly audit
  issue, whose fingerprint is constant and whose `updatedAt` moves on any touch,
  including a relabel. The recurrence that is genuinely invisible without a
  write, a fingerprint refiled as a new issue after the previous one closed, is
  the case the pair key and the linked follow-up cover.
- A non-open issue is reported and not stored, so `--state all` audits the
  board rather than backfilling fixed findings. `knowledge(action='store')`
  carries no `status` field, only `update` does, so a closed finding could only
  enter the graph as an `open` entry with no resolution condition: unfinished
  work to every later sweep until somebody closes it by hand. Backfilling one
  is therefore a deliberate two-step act, a store followed by a
  `knowledge(action='update', status=...)`, and not something this producer
  does on its own.
- Every record is stored as `discovery_type: bug_found` at severity `medium`.
  The bridge does not rank findings: `store` refuses high and critical without
  supporting conditions, and an automated pull has no standing to assert impact
  on a guard's behalf. What the upstream producer did claim is kept as text
  instead: `surface-findings.yml` declares severity and source in the issue
  labels, and those land verbatim on a `github-labels:` line in the record.
- No derived tag can be `ephemeral`, `temp`, `scratch`, `test` or `demo`. A CI
  finding is a durable claim about a defect rather than a point-in-time
  snapshot, and any one of those tags would have the lifecycle pass archive it
  after seven days, since `bug_found` is not a permanent type and so nothing
  would override the tag. The filter is an invariant in the code rather than a
  property of today's slug vocabulary, because the next guard may well be named
  for what it detects: `flaky-test-detector` alone would mint `test`.
- Tags are derived from the fingerprint, never hand-listed, so their
  readability follows the producer's slug. A guard slug yields the full
  fingerprint, the guard name and its class words
  (`orphan-push-guard-claude-dead-branch`, `orphan-push-guard`, `orphan`,
  `push`, `guard`). An issue from `surface-findings.yml` carries a 16-hex
  digest instead, so its only content tag is that digest, plus the provenance
  tags `ci-finding`, `github`, `ci` and `issue-<n>` that every record gets.
- The stored record points at the issue rather than mirroring it. The text that
  carried the marker, the body or one comment, is trimmed at 4000 characters
  with the issue URL kept as the full record. The untruncated title, the label
  names, the issue state, the created and updated timestamps, and which channel
  the marker came from are all recorded as their own lines, so deleting the
  issue leaves those plus the fingerprint and the trimmed text, and nothing
  beyond them.
- It is read-only on GitHub. It never opens, closes, comments on or relabels an
  issue, so closing a fixed finding stays a human act and the record of it
  stays in the graph.
