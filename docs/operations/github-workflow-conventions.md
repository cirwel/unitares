# GitHub Workflow Conventions

One delivery contract for **every** agent that pushes to this repo — Codex,
Claude (CLI), and Claude (web/cloud harness) — so that concurrent sessions
don't collide, and so the operator can predict whether any given session's
work *lands* or *waits*.

This is the canonical reference. `AGENTS.md` and `CLAUDE.md` carry a short
summary in their shared-contract block and point here for the detail.

## Why this exists

Before this convention, delivery behavior diverged by *entrypoint*, not by
intent:

- **`ship.sh` default (`auto`)** routed runtime code to a draft PR but
  direct-pushed docs/tests/"other" straight to the current branch — landing
  immediately, no PR.
- **`ship.sh --auto-merge`** opened a PR and enabled GitHub
  auto-merge-on-green ("the old behavior").
- **Claude on the web/cloud harness** was handed a fixed
  `claude/<topic>-<id>` branch and always parked a *draft PR*, bypassing
  `ship.sh` routing entirely.

So Codex tended to direct-push docs and could opt into auto-merge, while
Claude-on-web parked draft PRs that sat until a human merged them. With many
sessions running at once that is unpredictable: three branch-naming schemes,
and merge behavior that depended on which tool and which agent shipped the
change. This document collapses that to one rule set, and `ship.sh`'s default
`auto` route now opens a draft PR for every change (see *Delivery* below).

## The convention

### 1. Branch naming — one pattern, agent-prefixed

```
<agent>/<topic>-<short-id>
```

- `<agent>` is `claude` or `codex` — kept as a prefix so a branch is
  self-identifying at a glance, which matters when several sessions run in
  parallel.
- `<topic>` is a short kebab-case slug of the change.
- `<short-id>` is a timestamp or short hash that makes the branch unique.

Both existing generators already satisfy this shape:

- `ship.sh` mints `<agent>/auto/<timestamp>-<slug>` (the agent prefix is
  detected from `CLAUDECODE`, or set via `UNITARES_SHIP_AGENT`).
- The web/cloud harness hands Claude a `claude/<topic>-<id>` branch.

Never push to `main` or `master`. If you find yourself on the default branch,
create a feature branch first.

### 2. Delivery — draft PR for everything

Every session lands its work as a **draft PR**, regardless of agent and
regardless of whether the change is runtime code or docs/tests. The operator
is the merge gate.

- If the operator asks an agent to ship, finish, deliver, open a PR, or
  otherwise complete a delivery workflow, the agent may assume branch -> commit
  -> push -> draft PR is authorized. Do not stop for a second confirmation just
  to push the branch or open the draft PR.
- **Do not** direct-push to a shared branch.
- **Do not** enable auto-merge by default.
- A draft PR means "visible, not claiming merged." **Merging** is the
  operator's deliberate action. **Marking ready** is the working agent's:
  the agent that owns the PR declares readiness itself, once its validation
  actually passed — CI green, a completed review with findings addressed (see
  "Review workflow" below), and no collision with an in-flight branch. A
  neutral UNREVIEWED warning is not review completion.
- **Readiness is agent-declared, never operator-inferred.** The operator
  pressing merge in order cannot verify content and should not have to
  guess doneness: a PR still in draft is "still working — hands off," even
  when the diff looks finished, and nobody marks another agent's PR ready
  on its behalf. A draft whose owner went silent is a question for the
  owner (KG channel) first — the stranded-work audit will NOT surface it,
  since it skips branches with an open PR. If the owner cannot return,
  the operator may mark it ready as an explicit override, saying so in a
  PR comment: an override with stated rationale is a decision, not the
  blind inference this rule forbids.
  Ordering constraints the agent knows about ("merge after #N") belong in
  the PR body, so in-order merging acts on declared state. Rationale
  (2026-08-27): marking agent PRs ready on inference while the agent was
  still revising is the trigger shape of the post-merge orphan-push
  incidents — the human gate authorizes; the evidence lives with CI,
  reviews, and the merge-loss guards.

### Review workflow

"Review round joined" used to be prose: some PRs carried a
review in the body, some in a comment, most in neither, and nothing could tell
which. It is now a command and a status check, the way `test-cache.sh` made
the test run one.

- `./scripts/dev/review.sh` reviews the PR diff for `HEAD` in a fresh
  reviewer session, preferring the other model (Claude for `codex/*`, Codex
  otherwise) and falling back to the available provider, read-only, within a
  shared 30-minute budget, and posts a **review record**
  comment. `ship.sh` now waits for it on every PR push and prints the
  findings to its caller. After pushing another way, the **authoring agent**
  runs the same command without waiting for an operator prompt. If a review
  is already running, the command joins its result instead of treating
  "started" as "finished". A bounded wait that expires reports incomplete.
- The `review` check (`.github/workflows/review-gate.yml`) succeeds when a
  review for the PR's **current diff** is `CLEAN`, or `FINDINGS(n)` has
  dispositions. Unresolved findings produce `action_required`. Missing review
  or reviewer failure produces a **neutral UNREVIEWED warning**, with the
  author's next action. An outage is never a clean review or a failing test.
  This check replaces the legacy commit status; old heads may still show
  that historical status until the next push. GitHub branch protections are
  separate and are not changed by this workflow.
- Findings: fix and push (the new diff is reviewed, up to the
  [round cap](#round-cap)), or post rebuttals with
  `./scripts/dev/review.sh dispose <file>` — never drop one silently.
- A separate human or model code review of the actual diff can be recorded
  with `./scripts/dev/review.sh record <file> --reviewer-name <who> --independent`,
  where the file ends with `VERDICT: CLEAN` or `VERDICT: FINDINGS(n)`. The flag
  attests a separate reviewer examined this diff; it is not authentication.
  Consult remains advisory; do not turn advice into a verdict by adding a
  marker. Council/dialectic is optional escalation for consequential design
  choices or disagreement. Routine PRs need one completed code review. The
  gate needs no model and no paid key; CI only reads review evidence.
- The record is keyed on the diff (path + blob of every changed file against
  the merge base), not the commit, so a base merge that leaves the PR's files
  alone — including `draft-base-refresh.yml`'s — keeps it.
- The gate proves a review was recorded, not that it was honest: every agent
  posts through the same GitHub account, so a comment cannot distinguish a
  real review from an author's own. A record whose reviewer is the PR's own
  author is not a review.
- Bot PRs (dependabot) get no exemption: the incident that motivated "no
  mechanical exemption" was a dependency bump. Nobody has to remember them
  either: the optional `review_gate.py sweep` fallback reviews one quiet PR per run
  from the owner's account or dependabot, **including drafts**, after 15
  minutes without an update. A draft restricts readiness and merging; it must
  not prevent the review needed to reach readiness. `no-auto-review` holds
  automatic review of intentionally unfinished work. Outside contributors'
  PRs get a human first. A lock in the git common dir keeps the sweep and an
  author's review from running the same diff twice. Failed runs retry at most
  three times per provider per diff; unresolved findings and exhausted retries are reported
  as author follow-up in the sweep log, never silently treated as clean.

Quota, authentication, and startup failures put that provider on a one-hour
cooldown shared across worktrees; the other provider is tried immediately.
`--reviewer codex` or `--reviewer claude` explicitly retries after access is
restored. Findings stop routing: another model cannot erase an inconvenient
review. Separate output directories preserve each attempt.

Native Codex is an optional default for an operator who has enabled GitHub
code review. On this operator's UNITARES repo, native requests are owned by
the author/sweep workflow: repository auto review is **Follow personal**, and
the shared account's personal **Auto review is off**. Explicit native review
requests remain available, with exhaustive review and credit overage off.
Enable this integration in the shared local repository:

```bash
git config review.native true
```

The 2026-09-23 pilot exposed overlapping triggers: **Every push** reviewed
base updates and finished after #2352 merged; **On PR open** then started
another review when #2353 was marked ready, even though that exact commit
had already passed native review. The command-owned request avoids both
retriggers. Authors run `review.sh` for changed diffs as part of delivery;
the operator does not need to request reviews or chase their comments.

`review.sh` first reads native completion evidence for the current commit. If
nothing has started after 30 seconds, it posts one `@codex review` request
bound to the head and diff; this covers drafts that automatic review misses.
It waits up to ten minutes within the total review budget before using the
local fallback. Existing requests are reused, and an expired request is not
posted again on each sweep. `--reviewer` explicitly selects the local path;
`--fresh` can re-review clean evidence but cannot bypass unresolved findings.

The adapter recognizes the official Codex bot's submitted reviews and explicit
clean comments naming the reviewed commit. It also joins a completed activity
row naming that commit with the bot's clean PR reaction posted after that
completion; a stale reaction cannot approve a new push. It validates abbreviated hashes
against local git objects and invalidates evidence on a new head. Retargeted
PRs use local review because native artifacts identify the head but not the
reviewed base; even a completion arriving after retarget may have reviewed
the earlier base. A reaction or "Completed" activity row alone, and absence of
findings, are not sufficient. A completed native run is not requested again
while its evidence is arriving. Findings remain open across later clean results
or outages until individually disposed. CI consumes native evidence without
starting a model, regardless of the local opt-in setting. Author commands also
read existing native findings even when native dispatch is off. If GitHub review
history is unreadable, CI preserves its previous check and the author command
reports incomplete evidence; it never substitutes a partial clean result.
Reviewer availability and evidence availability are separate failures. A final
head-and-diff check runs after publishing any completion receipt, so a push
during that write also reaches the author as UNREVIEWED.

Joining a native clean result publishes a diff-bound review record once. This
triggers CI even when the clean reaction arrived after the activity comment's
workflow finished; reactions themselves have no workflow event. The quiet-PR
sweep also records completed native clean reviews. Published evidence renders
incidental bot mentions as plain names: copying a native footer's example
commands otherwise starts another cloud task from the author's account.
Local review commands stop
on closed or merged PRs and check again before publishing results. Native
cloud reviews already in flight can still finish after merge; triage any valid
late findings in a follow-up change rather than reopening the merged PR.

Native review focuses on major correctness issues. Consult is still useful
for focused design advice; council/dialectic remains an optional escalation.
Neither becomes an extra mandatory review step.

Pilot evidence: [draft #2340 native completion](https://github.com/cirwel/unitares/pull/2340#issuecomment-5794562451)
named its reviewed commit after an explicit request. Automatic review did not
start on the initial draft of #2352 during the pilot; the command-owned request
is therefore necessary for this workflow. See the PR for subsequent push and
fallback validation.

#### Round cap

A PR gets **three full review rounds** (`ROUND_CAP` in `review_gate.py`). A
round is one completed native Codex run, counted by the distinct commits
Codex names. Codex can post a result three ways: a submitted review, a clean
comment, or a completed activity row. All three count. These don't count: a
reply inside an existing thread, the receipt that records a native result, a
disposition, a fix verification, and a local fallback record. A local record
names no commit, no start time and no per-finding severity, and counting it
opened a new gap each time it was tried on #2401. After any base change the
cap no longer applies: the gate stops trusting native evidence then, because
a native run does not say which base it reviewed. The `review` check shows
the count ("review round 2 of 3").

Why: every run spends the same subscription quota that authoring does. In the
first ~14 hours of native review (2026-09-23/24) there were 137 Codex runs
across 27 PRs. Four PRs used 64 of them: #2380 (20), #2387 (18), #2378 (16),
#2376 (10). Twelve PRs finished in two runs or fewer. The long runs did not
converge because each fix added new text or code for Codex to flag. Those later
findings were usually valid but minor, and the quota they cost was the scarce
resource.

The rule is for fix loops only:

- A **P0/P1** in the last round always gets another full run after its fix.
- A **clean** last round is not a fix loop. A push after it is new work, and
  new work gets a full review.
- Past the cap, with only P2s open, `review.sh` does not request Codex and
  does not start the local fallback, which spends the same quota. The
  fallback runs only when native review is unavailable, and its own rounds are
  bounded by the review budget, not by this cap. Answer the
  remaining findings in one batch:
  - **Don't push:** dispose them on the reviewed diff with
    `review.sh dispose` (fixed later in #N, or rebutted). This costs no model
    call.
  - **Push fixes:** if `git config review.verifier` is set, `review.sh` asks
    that model whether each finding is addressed by the fix commits. It posts
    a diff-bound record with the reviewer `fix-verify:<model>`. Findings it
    judges unaddressed stay open as `FINDINGS(n)` for fixing or disposing.
    It checks the fixes only, not the new lines for new problems, and the
    record says so. A round is answered once it is disposed
    or its fixes are verified, and it gets only **one** answer: any push after
    that may be new work and gets a full review.
    With no verifier configured, a push past the cap is
    UNREVIEWED. The author says so and disposes, or deliberately spends a
    round.
- `review.sh --reviewer codex` (or `claude`) deliberately spends a round past
  the cap. Use it when the fixes changed enough that they need a real review.

The verifier is opt-in, like `review.native`. `ollama:<model>` runs on the
local Ollama server and costs nothing. `hf:<model>` uses the Hugging Face
router, which is metered, so it is never the default. On 139 real
finding/fix pairs from this repo, `gemma4:latest` caught 59 of 66 non-fixes
and passed 56 of 73 real fixes. So about 1 in 9 unaddressed P2s past the cap
is accepted as fixed. The record lists every finding it marked addressed, so
that can be spot-checked. The evaluation is in the PR that added the cap. On
this operator's machine, run once:

```bash
git config review.verifier ollama:gemma4:latest
```

#### Review provider availability

`scripts/dev/review_providers.json` is the one repo-wide switch for reviewers
that are down. A provider listed under `disabled` is skipped by
`review.sh`'s default choice, by its local fallback, and (for Codex) by native
review, in every checkout, so an outage no longer costs each session a failed
attempt per hour of cooldown. Agents without local tooling read the same file
before posting `@codex review`. An explicit `review.sh --reviewer <name>`
still tries a disabled provider. Re-enable it by deleting its entry.

**Antigravity (`agy`) as a reviewer.** `review.sh` can review with Google's
Antigravity CLI on the operator's subscription login, with no API key. It is
used only when the `agy` command is installed. The ordering prefers a model
family other than the branch's author prefix: for a `claude/` branch it is
Codex, then Antigravity, then Claude, minus anything disabled above.

It is deliberately **not** run inside the checkout. A PR can carry
`.agents/` hooks, rules and project permission rules, and `agy` loads those
from its working directory. That is the same class of hole that ruled out the
Gemini CLI (#2423, reverted). So `agy` runs in a **temporary workspace**
that holds only the review material: `diff.patch` and, under `files/`, the
full post-change text of every changed file at its repository path. It reads
them with its own file tool, which needs no permission grant inside its
workspace, and never executes them. A changed file whose name `agy` would load
as instructions (`AGENTS.md`, `GEMINI.md`, `CLAUDE.md`, anything under
`.agents/`, `.agent/` or `.gemini/`) is written with a `.review-copy` suffix.
Two changed files that would land on the same path keep the first, and any file
not placed is listed in `omitted.txt`. It also gets
a fresh temporary `HOME` (only the login keychain is linked in), so the
operator's standing permission grants and MCP servers in `~/.gemini` never
load, plus `--mode plan --sandbox`. (`--disable-slash-commands` is not used:
`agy` 1.2.11 switches plan mode off when it is set.) The trade-off is
that the reviewer cannot browse the rest of the repository. There is no size
limit: the material used to be inlined as one command-line argument, capped
at about 120 KB, and PR #2470's 143 KB diff could not be reviewed at all. The
default model is Gemini 3.1 Pro (High); `REVIEW_AGY_MODEL` overrides it
(`default` leaves the choice to `agy`). It was chosen on two PRs, where it was
the only model that found real defects, so treat it as a starting point. A
shell command `agy` reaches for is denied and the conversation is resumed with
a reminder that only file reads work.
Content is read only from committed git objects: symlinks, submodules and paths
outside the repository never reach the prompt. The dialectic reviewer can use
Antigravity too (`UNITARES_DIALECTIC_REVIEWER_HOST=antigravity` in the
orchestrator's environment), but through its own backend: it inlines a short
self-contained prompt, keeps the ~120 KB argument cap, and does not read
`REVIEW_AGY_MODEL`. Setup on a machine:

```bash
curl -fsSL https://antigravity.google/cli/install.sh | bash
agy        # once, interactively: sign in, then /exit
./scripts/dev/review.sh --reviewer antigravity   # or let the default pick it
```

Antigravity's terms let Google collect interaction data unless you opt out in
its settings. A review sends the diff and the changed files.

Operator, 2026-09-25: Codex is unavailable indefinitely (the OpenAI account
was suspended). Until that entry is removed, review with a fresh-context
Claude subagent or council, or another model such as Gemini, and record it
honestly ([Recording a review without gh](#recording-a-review-without-gh)
when `gh` is missing).

#### Requesting review without local tooling

**Check [provider availability](#review-provider-availability) first:** while
Codex is disabled there, skip this section and use
[Recording a review without gh](#recording-a-review-without-gh).

`review.sh` needs the `gh` CLI and a local Codex or Claude CLI, so it cannot
run in cloud sessions, sandboxed runtimes, or any environment without them.
When native Codex review is enabled for the repository (it is on this
operator's), the environment-independent path is a PR comment:

1. Post `@codex review` as a top-level comment on the PR. Any agent that can
   write a GitHub comment (through the `gh` CLI, a GitHub MCP connector, or
   the REST API) can do this, and drafts are covered.
2. The `review` check re-runs on the Codex bot's comment and reads its result
   directly: a clean result becomes `clean (codex-native)`, and findings
   appear as review threads.
3. **Fixing** a finding also works without tooling. Push the fix, then post
   `@codex review` again: native review here triggers on PR open, not on
   every push, and the new diff needs its own result. **Rebutting** a finding
   needs a diff-bound disposition record; a thread reply is not read. Without
   `gh`, reply on the thread with the rebuttal, then render the record with
   `review.sh dispose <file> --emit` as described in
   [Recording a review without gh](#recording-a-review-without-gh) and post it
   verbatim. Do not assemble the disposition record by hand.
   The [round cap](#round-cap) applies here too. After three rounds with only
   P2s open, do not post `@codex review` again: hand the remaining findings
   off for disposition the same way. A P1 fix still gets its request.
4. If Codex replies "Something went wrong" (for example `Provided git ref …
   does not exist` right after a push), post the request once more. That
   error came from Codex's checkout lagging the push on 2026-09-23 (#2356);
   the retry succeeded. A second identical failure is an outage to report,
   not something to loop on.

Do not hand-build a `unitares-review v1` record in place of this. The gate
trusts records from the owner's account, which every agent posts through, so
a hand-assembled record is indistinguishable from a real one (#2356 posted one
and retracted it). `review.sh` remains the path when native review is not
enabled, and the fallback when Codex is unavailable. Without `gh` either, use
the next section.

#### Recording a review without gh

Operator decision, 2026-09-24: an agent without `gh` (a cloud session with
only a GitHub connector) may complete its own review gate with a **subagent
or council review**, as long as the record says which it was. Before this, a
Codex usage limit left such sessions with no way to finish a PR (#2423).

1. Run the review in a **fresh context** that did not write the diff: a
   subagent given only the diff and `REVIEW_PROMPT` from `review_gate.py`, or
   a council/dialectic reviewer. It must end with the `VERDICT:` line.
   Advisory `consult` output is still not a review.
2. Push first. Then render the record with the tool, never by hand:

   ```bash
   ./scripts/dev/review.sh record review.txt --independent --emit \
       --reviewer-name subagent:<model>-fresh-context   # or council:<who>
   ```

   `--emit` needs no `gh`: it computes the diff key locally, refuses a HEAD
   that the remote branch of the same name does not hold, and prints the
   exact comment body. It keys against `origin/master`; for a PR based on
   another branch, add `--base origin/<base>` or CI will never match it.
3. Post the printed body **verbatim** as a top-level PR comment through the
   connector. The `review` check reads it like any other record, and its
   description names the reviewer, so a same-session subagent review is
   visible as one.
4. Findings: fix, push, and review the new diff the same way. To rebut
   instead, render the disposition without `gh`:

   ```bash
   ./scripts/dev/review.sh dispose dispositions.txt --emit \
       --findings <n> --reviewer <reviewer from the open record> --cites <its URL>
   ```

   The file needs one numbered entry per finding (`1. fixed in <sha>` or
   `1. rebutted: <why>`). Offline the tool cannot read the open record, so you
   name it. CI checks the diff key, the count, and a native-review
   (`#pullrequestreview-…`) URL; a wrong one leaves the findings open. It does
   **not** verify `--reviewer` or any other `--cites`: it answers the latest
   open record with that count and displays the reviewer you passed. Copy both
   from the open record exactly, or the record misattributes.

A same-model subagent is the weakest reviewer this gate accepts: it shares the
author's model and blind spots. Prefer native Codex or another model when one
is available, and name the reviewer honestly in `--reviewer-name`.

The working agent reads the result, addresses findings, waits for CI, and
marks **its own** PR ready before declaring completion. A detached review
(`review.sh --background`) is useful while the agent does other work, but the
agent must call `review.sh` again to join before leaving. `SHIP_NO_REVIEW=1`
explicitly defers this step and prints the author's next action. If review
cannot finish, exit 2 distinguishes an **UNREVIEWED** handoff from findings
(exit 1). Report the blocker and the exact command to resume; keep the draft.
The sweep
supplies a missing review; it does not fix code, declare readiness, or merge.

The fallback needs an actual scheduler installation. On the operator's Mac:

```bash
python3 scripts/ops/install-review-sweep.py --repo ~/projects/unitares
launchctl print gui/$(id -u)/com.unitares.review-sweep
unitares-automations census --grep review-sweep --all
```

This opt-in installer copies a small bootstrap outside the development
checkout, loads a 30-minute launchd job, and records run outcomes through
`unitares-automation-run` when installed. It uses existing CLI authentication.
Each run refreshes a locked trusted checkout from `origin/master`; a separate
locked checkout supplies PR context. Logs are in
`~/Library/Logs/unitares-review-sweep.log`. Merely having `review-sweep.sh` on
disk is not evidence that the job is installed or running.

The `review` check describes review evidence; it is separate from the test
suite. A neutral warning asks the author to start/join review and links here.
Readiness still requires completed review. The sweep does not promote a draft
merely because tests passed or because a warning is nonblocking.

`ship.sh` enforces this. Its default `auto` route now opens a **draft PR for
every change** — runtime, docs, or tests:

```bash
./scripts/dev/ship.sh "type(scope): concise message"
```

- If all current worktree changes belong in the PR, use
  `./scripts/dev/ship.sh --stage-all "type(scope): concise message"` to stage,
  branch if needed, commit, push, and open the draft PR in one command.
- Runtime and detached-HEAD work mint a fresh agent-prefixed branch and open
  the draft PR there.
- Non-runtime work on a named feature branch opens the draft PR on that branch.
- `./scripts/dev/ship.sh --plan "..."` previews the route without shipping;
  `--stage-all --plan` previews the route for the full dirty worktree without
  mutating the index.
- `--direct` is the opt-out, for docs/tests-only pushes where you knowingly
  skip the PR.
- `--auto-merge` remains available for the rare case where the operator
  explicitly wants auto-merge-on-green; it is not the default.

### 3. Parallel / simultaneous work

This convention exists because a lot of work happens concurrently. Two
guards keep concurrent sessions from clobbering each other:

- **Single-writer surfaces** (migrations, identity/onboarding, `plan.md`, hot
  RFC docs, large test consolidations): before touching one, check for an
  in-flight PR and branch from its head instead of starting a parallel
  attempt. The authoritative list lives under *"Before Starting Work on a
  Single-Writer Surface"* in the `AGENTS.md` / `CLAUDE.md` shared contract.
- **Correcting an architecture fact in prose**: the same PR greps
  reader-facing docs (README, `docs/` outside `proposals/`, `skills/`,
  `AGENTS.md`/`CLAUDE.md`) for the old claim and updates every copy, and adds
  a row + deny-pattern to the Contested Claims Registry in
  `docs/dev/CANONICAL_SOURCES.md` so `check_doc_health.py` blocks the stale
  wording from reappearing. Corrections that land in one doc and drift in the
  others were the entire defect class of the 2026-07-02 coherence audit.
- **Changelog entries**: add a `docs/changelog.d/` fragment, never edit
  `docs/CHANGELOG.md` directly in an ordinary PR; the format is in
  [`docs/changelog.d/README.md`](../changelog.d/README.md).
- **Branch hygiene**: stale and superseded branches are swept per
  `docs/operations/branch-hygiene-runbook.md`. Branches with unique local work
  (`git cherry master <branch>` showing `+`) are held for review, never auto-
  deleted — so parking a draft PR is always safe.

### 4. Landing work — do not babysit the queue

`master` requires branches to be up to date before merging (`strict`), and
`enforce_admins` is on, so nobody can bypass it. With seven required checks and
a ~15-minute slowest job, **every merge invalidates every other open PR**. At
nine open PRs that is nine update-branch clicks and over two hours of CI per
pass through the queue, and each merge re-dirties the rest. That is arithmetic,
not a discipline problem — no amount of care makes it cheaper.

**Use `gh pr merge --auto <n>` instead of watching.** The repo now has
"always suggest updating pull request branches" enabled, so with auto-merge set
GitHub updates the branch itself when the base moves and merges as soon as
checks pass. This does **not** weaken the human merge gate: `--auto` is a
deliberate per-PR act, and it says "this one is approved, land it when green" —
you are giving up the waiting, not the decision. Draft PRs cannot take
`--auto`, so mark ready first; that mark is the gate.

**Confirmed working end-to-end 2026-08-14.** Two armed PRs were fixed and left
alone: #1653 merged at 09:45; #1658 went `BEHIND` the moment it did, and GitHub
moved its head on its own about two minutes later, re-ran CI against the fresh
base, and merged it at 10:08. No script and no human touched the branch in
between. **So do not write or run an update-branch babysitter for this repo** —
polling and pushing only races GitHub's own updater and burns a CI cycle per
redundant update. `unitares-governance-plugin` is the exception that still needs
manual `gh pr update-branch`, because auto-merge is disallowed there.

A workflow that predates this, `.github/workflows/pr-queue-autoupdate.yml`, was
removed in the same pass. It was a poor-man's queue added before the repo
setting existed, it required a PAT (`PR_AUTOUPDATE_TOKEN`) that was never
created, and so every run since — on each push to master plus hourly — exited
early having done nothing. Restoring it would put a second updater in a race
with GitHub's native one.

**Drafts are the one case GitHub's updater never covers** — a draft cannot take
`--auto` — so `.github/workflows/draft-base-refresh.yml` merges base into any
open draft that is behind, conflict-free, and idle for 12h, every four hours.
It never marks ready or merges, and the `no-base-refresh` label opts a PR out.
It is not a second updater racing the native one: it touches only what GitHub
will not. Two rules carried over from the retired queue updater and from PR
#2250 (2026-09-16). It pushes with the `DRAFT_BASE_REFRESH_TOKEN` repository
secret — a fine-grained token scoped to this repository with `Contents: read
and write` — because a `GITHUB_TOKEN` push creates the head's `pull_request`
runs in the approval-required state: #2250's refreshed head sat `blocked` with
eight parked workflows until a maintainer approved them by hand, less
mergeable than the stale head it replaced. And a missing secret is loud rather
than inert: the sweep withholds every push and fails the run, and after each
real push it fails unless a `Tests` run is actually queued on the new head.

**Do not stack more than two deep.** Each level must land in order, and every
merge below re-dirties everything above. A three-deep stack built 2026-08-13
produced a conflicted middle PR within hours, purely from its own base moving.
If two changes touch the same file, they are one PR — splitting them buys
reviewability and pays for it in cascade.

**Land or close drafts quickly.** An open draft accrues cascade debt: it needs
a rebuild every time anything merges, whether or not anyone is working on it.

### Merge queue — the real fix, and what blocks it today

A merge queue is the native answer: it tests each PR against the *projected*
result of the ones ahead and merges in order, so `strict` stays on and nobody
hand-updates anything.

**Do not enable it yet.** A merge queue only advances when the required checks
report on `merge_group` events. Five of the seven do — `tests.yml`
(smoke / test / dashboard), `repo-scope.yml` (scope), and
`documentation-validation.yml` (validate) all carry a `merge_group` trigger.
The remaining two, **`Analyze (actions)` and `Analyze (python)`, come from
CodeQL default setup**, which has no workflow file to add a trigger to and does
not run on `merge_group`. Enabling the queue against those two as required
checks means every entry waits forever for a check that will never arrive —
i.e. it blocks all merges rather than speeding them up.

Two ways to unblock, whichever is preferred:

1. Drop the two `Analyze` checks from *required* status checks. They still run
   on every PR; they just stop gating the merge.
2. Convert CodeQL from default setup to an advanced-setup workflow file and add
   `merge_group` to its triggers.

Verify with `gh api repos/cirwel/unitares/code-scanning/default-setup` before
assuming this note is still current.

### Merge-loss guards — the detective layer

Until (and after) a queue exists, three repo-side workflows make the known
silent loss modes loud. They are detective, not preventive: each fails a run
and files/updates a deduped `ci-finding` issue at the moment a loss becomes
visible, instead of leaving it to be discovered weeks later. Server-side on
purpose — client-side harness hooks bind one agent; a workflow binds every
pusher.

| Workflow | Loss mode it surfaces | When it runs |
| --- | --- | --- |
| `orphan-push-guard.yml` | Commits pushed to a branch after its PR merged/closed (three confirmed incidents, 2026-08-12/19) — real-time counterpart of the weekly `stranded-work.yml` audit | Pushes to `claude/**` / `codex/**` on branches cut after the workflow landed (push workflows run the pushed ref's definition; older branches keep weekly-audit coverage) |
| `merge-content-check.yml` | A merge whose head lacks the branch's newest recorded push (the stale-head variant of PR #1610); a push that *postdates* the merge routes to the orphan-push finding instead — and since this runs from the base side, that covers old branches too | Every merged PR into master |
| `automerge-disarm.yml` | Auto-merge silently disarmed by a transient check failure, stranding an armed PR (PR #1476). Label a PR `automerge-hold` to mute a deliberate hold | Every 6h; one tracking issue updated in place |

All three share one rule (see `scripts/ci/merge_loss_common.py`): they fail
open on API errors so a broken guard never blocks delivery, but a degraded
run always says so — `::warning::` plus a step-summary line — because a
guard that fails toward "healthy" is this repo's named recurring failure
mode. Two honesty notes baked into the wording they emit: a clean
`merge-content-check` pass says "no contradiction found", not "verified"
(the events feed it reads lags 30s–6h and retains ~300 events, which can
hide a final push but cannot fabricate a false alarm); and the absence of
an `orphan-push-guard` run on an old branch is a coverage gap, not a
clean verdict.

If `orphan-push-guard` fails your push: stop pushing to that branch. The
work is not lost — follow the cherry-pick recipe in the issue it filed
(fresh branch off `origin/master`, new PR). A dead-branch push whose
commits are all already landed is classified PRUNABLE and passes without
an issue. If you are deliberately reusing a branch name for a new round
of work, the guard fires until the new PR opens — open it and close the
finding as a false positive (fresh `<author>/<topic>-<id>` names avoid
this entirely).

## Quick reference

| Situation | Do this |
| --- | --- |
| Ship any change (Codex or Claude CLI) | `./scripts/dev/ship.sh "msg"` — defaults to a draft PR |
| Ship the whole dirty worktree | `./scripts/dev/ship.sh --stage-all "msg"` |
| Operator asks to ship/finish/deliver/open PR | Branch, commit, push, and open the draft PR without an extra confirmation |
| Preview the route first | `./scripts/dev/ship.sh --plan "msg"` |
| Claude on the web harness | Already parks a draft PR on its `claude/...` branch — nothing extra needed |
| About to touch a single-writer surface | Check for an in-flight PR first; branch from its head if one exists |
| Operator explicitly wants auto-merge | `./scripts/dev/ship.sh --auto-merge "msg"` (not the default) |
| A READY PR should land unattended | `gh pr merge --auto <n>` (readiness was the owning agent's declaration; see section 2) |
| Tempted to stack a third PR on a stack | Fold it into the one below instead |
| Review round 3 done, only P2s open | Dispose them in one batch; don't request round 4 ([round cap](#round-cap)) |
| Docs/tests-only, knowingly skipping the PR | `./scripts/dev/ship.sh --direct "msg"` (the opt-out) |

## Per-entrypoint mapping

- **Codex (CLI):** stage, then `ship.sh "msg"` — its default `auto` route opens
  a draft PR for every change. Report the delivery line at closeout
  (`/closeout`). Use `--direct` only for docs/tests-only pushes you knowingly
  want to skip the PR for.
- **Claude (CLI, plugin harness):** same as Codex — `ship.sh "msg"`.
- **Claude (web/cloud harness):** the harness already enforces the convention
  (fixed `claude/...` branch + draft PR). Keep work on that branch; let the
  draft PR be the delivery artifact.
