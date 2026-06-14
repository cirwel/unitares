# CI Issue Surfacing (experiment)

An experiment in wiring UNITARES's "issue surfacing" instinct into GitHub CI:
run deterministic collectors on a PR / on demand, and turn each *new* finding
into a deduped GitHub issue.

This is the CI-side counterpart to the in-server surfacing agents
(`agents/watcher`, `agents/vigil`). Those need a live governance server and an
LLM detector; a stock GitHub runner has neither. So the CI bridge uses
collectors that run on a vanilla `ubuntu-latest` with no Postgres, no Ollama,
and no secrets beyond `GITHUB_TOKEN`.

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
