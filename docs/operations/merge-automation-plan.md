# Autonomous Merge Conductor

**Status:** implemented, report-only by default. Autonomous execution remains
flag-gated until the rollout gates below are met.

## Outcome

After rollout activation, routine PRs no longer need a human to click Ready,
Update branch, and Merge. The author declares its work complete; a separate
reviewer process judges the exact Git head; deterministic gates enforce
repository policy; GitHub performs the protected merge. While execution and
the required check remain dormant, the queue marker is inert and a human
maintainer remains the merge gate. After activation, human attention is
reserved for changes that redefine the gate or production authority.

```text
author -> queued draft -> current branch + CI -> independent reviews
       -> App-bound agent-review check -> Ready -> fresh gate read -> native auto-merge

root/control change ----> root App approval -> 2 reviews
                    \----> no App: paused root maintenance window
conflict / red CI / disagreement --------> hold or escalation
```

The implementation is `scripts/ops/merge_conductor.py`. The historical
`com.unitares.pr-babysitter` LaunchAgent keeps its label and shell entrypoint for
report-only migration shadow, but it is not an execution boundary. The source
plist template is now an isolated system LaunchDaemon running a dedicated
unprivileged account. An already-installed same-user plist has no conductor
flags and therefore selects the safe report-only default as soon as the new
entrypoint lands.

## Trust boundaries

The conductor does not let an authoring model merge its own work.

- A `codex/*` branch is reviewed by Claude first, then Codex.
- A `claude/*` branch is reviewed by Codex first, then Claude.
- The attributable defaults are Claude `opus` and Codex `gpt-5.6-sol`;
  `UNITARES_MERGE_CLAUDE_MODEL` and `UNITARES_MERGE_CODEX_MODEL` override them.
  Claude's provider envelope records every routed model (including helper
  models). Codex currently records the explicit request and emits a provenance
  warning because its CLI JSON stream does not report the routed model.
- Every change needs both model families. Branch-prefix attribution only chooses
  which family reviews first; it cannot reduce quorum because it is not
  authenticated authorship evidence.
- Every change needs two fresh review contexts. Each receives the byte-identical
  immutable patch, metadata, and deterministic evidence prompt. Neither call
  receives the other reviewer's verdict, findings, or summary before it decides;
  the prefix chooses ordering only.
- A same-repository branch without a `codex/` or `claude/` prefix is treated as
  human/imported provenance and still receives both reviews in stable
  Claude-then-Codex order. No family identity is guessed.
- Branch-prefix provenance is author-controlled routing metadata, not proof of
  model identity. The heterogeneous review policy is a cooperative-process
  control under the documented single-operator threat model.
- Reviewer tools are disabled, not merely described as unavailable. Claude uses
  safe mode, no user/project/local setting sources, an explicitly empty strict
  MCP configuration, no Chrome integration, and an empty tool allowlist. Its
  retained subscription-auth HOME belongs to a dedicated reviewer UID, not the
  conductor or an author. Codex uses that same explicit reviewer HOME rather
  than an implicit passwd-database fallback, starts with strict config; disables shell,
  unified execution, multi-agent, image, hook, and skill-install capabilities;
  disables web search; receives a secret-free environment allowlist; and fails
  closed if its JSONL stream nevertheless contains a tool event. Both reviewers
  run from an empty temporary working directory with no session persistence. PR
  prose and patches are explicitly delimited as untrusted evidence with a fresh
  unpredictable boundary/verdict
  nonce. This prevents replayed/static patch JSON or a copied terminator from
  being parsed as the verdict; it does not make a model immune to in-context
  instruction attack, so unanimous review and deterministic gates still carry
  the decision.
- The privilege-separated worker returns the trusted nonce and a SHA-256 of the
  exact prompt in its outer envelope. The conductor recomputes and checks both
  before accepting the inner verdict, so a stale or mismatched worker response
  cannot be attributed to the current review call.
- A malformed, inconsistent, unavailable, or incomplete verdict cannot approve.
- Before any model call, the resident invokes a root-deployed review worker
  through a narrow passwordless sudoers rule as that second UID. The worker
  verifies its UID, mode-`0700` HOME, exact root-attested CLI paths/versions, and
  every isolation flag on which the adapters rely. Startup also asks the worker
  UID to open the conductor credential directory, App key, and secrets file and
  fails unless all three reads are denied.

Bootstrap evidence captured on 2026-08-17 against the installed clients:
Claude `2.1.233 (Claude Code)` advertised `--safe-mode`, empty
`--setting-sources`, empty strict MCP configuration, `--tools`, `--no-chrome`,
and `--no-session-persistence`;
Codex advertised `--ignore-user-config`, `--ignore-rules`, `--strict-config`,
`--disable`, `--config`, `--sandbox`, `--skip-git-repo-check`, `--ephemeral`,
`--json`, and `--output-last-message`, plus every named feature the adapter
disables. The pinned Codex client is `codex-cli 0.147.0`. Execute/review mode
compares both exact version strings, repeats these help and feature-contract
checks on every process start, and fails closed on version drift or a removed
surface.
The opt-in behavioral canary also passed for both backends on 2026-08-17: an
untrusted patch instructed each model to write an external marker and fetch a
loopback URL; neither marker appeared, the listener received zero requests, and
the adapters observed no tool event or permission request.

Flag presence cannot prove that a future CLI release preserves the same sandbox
semantics. Residual-risk owner: the repository operator. Any Claude or Codex
CLI binary/version change is a rollout stop: set conductor execution to `0`,
update the pinned version only through a root/control change, repeat a live
canary that asks the isolated reviewer to write outside its temporary directory
and invoke an external URL, record that neither effect occurred, rerun the
help-contract preflight, and only then restore execution. This behavioral
revalidation is operational because provider-backed model calls are
intentionally not part of deterministic CI.

Model independence remains contextual and procedural, while approval
publication has both a distinct GitHub identity and two OS credential boundaries.
The ordinary author credential cannot satisfy the gate: only the
dedicated review GitHub App can create the `agent-review` check run, and branch
protection is pinned to that App's numeric ID. The App installation token is
minted in memory from a service-owned mode-`0600` private key, scoped to this
repository and `checks:write`, cached only until shortly before its one-hour
expiry, and passed to `gh` through its environment rather than argv. Reviewer
subprocesses run as a different UID and receive neither the key nor the token.
Execute mode refuses to run unless
`/etc/unitares/merge-service-boundary.json` is root-owned and attests distinct
conductor, reviewer, and author UIDs; disjoint service/reviewer HOMEs and
conductor credential root; fixed non-author-writable provider binaries and
worker; isolated/no-site Python plus every import root; the real author claim
registries; the exact App key and secrets file; and a deploy tree no author can
write. The historical same-user LaunchAgent therefore cannot be activated.
The root-approver App remains a separate service/host and its key must never be
placed in either merge service.

Ordinary PR reads and state transitions use a second, repository-scoped
fine-grained PAT supplied only as `UNITARES_MERGE_SERVICE_GH_TOKEN`; isolated
service shadow and execute modes never inherit an ambient `gh` login. Its
required profile is exactly
`Administration: read`, `Pull requests: write`, `Contents: read`, and
`Checks: read`, and `Commit statuses: read` (plus GitHub's implicit Metadata
read). It must have neither
Administration write nor Contents write, so it can inspect protection and arm
native auto-merge but cannot patch protection, push arbitrary commits, or call
the direct-merge REST endpoint. The one server-side branch mutation is GitHub's
`PUT /pulls/{number}/update-branch`, called with `expected_head_sha`; for a
fine-grained PAT GitHub documents that endpoint under Pull requests write (the
additional head-repository Contents write note applies to GitHub App tokens,
which the ordinary service path does not use). GitHub likewise documents Pull
requests write as sufficient for adding/removing labels on a pull request and
posting its issue-timeline comment; Issues write is not part of the resident
profile. The root/operator admin token exists only in the declared setup or
rollback window and is never stored in the resident secrets file.

Root authority is separate again: the latest label event must carry the
configured root-approver GitHub App ID, and that independently operated App must
publish a successful `agent-root-approval` check on the exact current head SHA.
The review App must not be used as the root-approver App. A shared-user event or
an approval check on an older head cannot authorize root automation; startup
fails closed when the two configured App IDs are equal. If a root-approver App
ID is configured, execute preflight also requires `agent-root-approval` to be a
branch-protection check pinned to that App. The root service must emit a neutral
or successful not-applicable result for ordinary heads and a successful explicit
approval for root/control heads. This makes a newer failed/revoked root check a
merge-point veto rather than only a conductor-time observation. Within the
conductor, a successful review check is never a cache for an unarmed PR: it
always runs a fresh current-process review before arming.

Reviewers return one of `approve`, `deny`, `needs_evidence`, or `escalate`.
Only unanimous `approve` satisfies the gate.

## Queue intent

Draft still means the author has not granted merge authority by itself. A PR
enters the conductor only when it contains ordinary queue intent:

- the marker `<!-- unitares-merge-intent: autonomous -->`, which `ship.sh`
  adds on its default route; or
- the `merge:auto` label, useful for web/cloud-agent PRs and existing drafts.

`merge:root-approved` is a separate authority signal and never queues a PR by
itself. A root PR needs ordinary queue intent, a label event from the App ID
configured by `UNITARES_MERGE_ROOT_APPROVER_APP_ID`, and that App's successful
`agent-root-approval` check on the exact head. Every push therefore requires a
new root approval check.

`ship.sh --draft-pr` creates an unqueued draft. `merge:hold` overrides every
other signal and is the per-PR kill switch. `merge:escalate` parks a PR after a
policy or review refusal.

Because `agent-review` is a globally required branch check after activation,
an unqueued PR is intentionally not mergeable yet. For a human/imported branch
in the same repository, a maintainer adds `merge:auto`; arbitrary branch names
then receive the same two-family quorum in stable Claude-then-Codex order. A
fork PR cannot receive a base-repository App check safely; mirror its reviewed
head to a same-repository branch and queue that PR, or use the OS-root
maintenance window. This is the post-activation human/external contribution
path, not an administrator click-through.

When default shipping adds queue intent to an existing PR, it uses the
set-valued `merge:auto` label rather than a read-modify-write of the PR body, so
a concurrent maintainer body edit cannot be overwritten. It does not silently
reverse a Ready or armed maintainer state during the dormant rollout.
After activation, the conductor owns parking: it publishes in-progress,
disables auto-merge, restores draft, and verifies that state before fresh
review. Explicit manual `--draft-pr`/`--open-pr` routes still disarm because the
operator selected that state deliberately.

## Deterministic gates

Before spending a model review or changing PR state, the conductor requires:

1. A user-private non-blocking process lock under `~/.cache/unitares/` prevents
   overlapping five-minute/manual cycles on one host. Execute mode additionally
   acquires one atomic lease-plane mutex for the repository and protected branch
   (`maintenance:/merge_train/...`) before inspecting or mutating the train.
   This shared-Postgres lease serializes hosts with different local lock files;
   an unavailable, unauthorized, or already-held lease fails closed. Ownership
   is atomically renewed before the queue-readiness protection read, before
   each candidate probe, before review starts, before each sequential model
   call, immediately before approval publication, before Ready, and once more
   before the auto-merge arm call. Losing ownership at any boundary stops that
   process; after loss it performs no compensating writes. The lease remains
   held until the target is observably armed or observably merged. The train is
   therefore serial across conductor hosts even when GitHub merges immediately
   in response to the arm call. The lease cannot serialize a maintainer or
   unrelated credential acting directly on GitHub. Such actors must not arm a
   queued PR while execution is live; the conductor reads the armed queue both
   immediately before and after its arm request. A pre-arm collision parks the
   target, and an observably armed target is disarmed on a post-arm collision.
   If GitHub already merged the target while another PR became armed, the cycle
   reports an invariant error but cannot undo that merge. GitHub exposes no
   atomic compare-and-arm predicate for this local invariant, so branch
   protection remains the merge-point safety authority if an external actor
   lands in that final network race.
   Serial mutation does not imply head-of-line blocking: a read-only readiness
   scan skips queued drafts with transient red/pending CI, unresolved threads,
   unknown mergeability, or a behind branch when a ready peer exists. A failed
   update request returns a recoverable result naming that PR; it cannot make an
   already-ready peer wait behind it. Policy violations are still selected so
   they can be visibly escalated.
2. No active repository surface claim. The isolated service does not run Git
   in its root-owned deploy checkout or use its private HOME as the claim
   source. The root manifest binds exactly one readable author-owned
   `git-surface` state registry per author UID and the `owner/repository`
   identity; the conductor scans active metadata by normalized GitHub remote,
   so claims from every worktree are included. An unreadable, malformed, or
   unrecognized active claim fails shut; absence is never inferred from a
   failed probe.
3. Same-repository PR targeting the configured protected branch and confirmed
   mergeability (not unknown or conflicting). A Codex/Claude prefix selects
   review order only; an arbitrary same-repository branch receives the stable
   Claude-then-Codex order.
4. Branch current with `master`. A `BEHIND` queued draft cannot use GitHub's
   armed-PR updater yet, so the conductor requests one update and leaves review
   for a later cycle. An already-armed PR is first left to GitHub's native
   updater. If the same head remains behind for 15 minutes, a user-private
   persistent timer permits one guarded `update-branch` fallback and resets only
   after the request succeeds. Both queued and fallback requests call the
   pull-request update endpoint with the head SHA observed before the request;
   a raced push is rejected rather than updating an unreviewed head. A failed
   update is recoverable and never becomes an unlabelled exception or terminal
   review verdict.
5. The live branch-protection set is read before review. Every required
   non-review `(context, GitHub App ID)` identity must have appeared and
   completed successfully; a same-named check from the wrong App or a passing
   subset is still a wait. The set and provenance-bearing check runs are fetched
   again after review so a newly required or delayed context prevents Ready/arm.
   Non-required/advisory checks remain visible in GitHub but do not become an
   undocumented permanent queue gate.
6. All review conversations resolved. More than 100 threads is an explicit
   partial-read refusal, not a silent zero.
7. At most 80 changed files, 8,000 changed lines, a 128 KB PR body, and a 1 MB
   review patch. The title/body are included inside the same untrusted-evidence
   delimiter as the file list and patch so reviewers receive the author's stated
   intent without treating it as instructions.
   Both the file list and patch come from GitHub's immutable
   `compare/<base-sha>...<head-sha>` endpoint; mutable PR-number diff endpoints
   are never review evidence. The patch requests
   `application/vnd.github.diff`, so reviewers receive one unified comparison
   rather than a commit-by-commit format-patch series. The JSON comparison must
   report the requested base and head SHAs or the cycle fails closed. Commit
   pages are fetched at 100 per page and the returned commit count must equal
   `total_commits`; this avoids GitHub's default 30-commit page becoming false
   head evidence. GitHub documents that the comparison's complete changed-file
   list appears only on page one, capped at 300 rows, so the conductor reads
   files only there and keeps its policy limit at 80.
   The immutable compare response must match GitHub's reported changed-file
   count. Because the policy limit is 80 files (below the compare endpoint's
   response ceiling), larger, partial, or binary-only evidence is escalated
   instead of truncated into a false approval. Both source and destination paths
   of a rename are classified, with the stricter tier winning. Root approval
   cannot waive an evidence-completeness limit.
   Comparisons with more than 250 commits are explicitly escalated rather than
   relying on GitHub's truncated commit array or waiting forever. Both reviewers
   receive this same frozen evidence prompt independently; the second reviewer
   never receives the first reviewer's answer.
8. Immediately before Ready, the conductor rechecks head and base SHA,
   intent/hold/root labels, mergeability, CI, review threads, surface claims,
   the exact review-App check, and the one-armed invariant. Check-run lookup
   paginates rather than treating the first 100 records as complete. The mutable
   authority/CI/thread/claim set is checked once after model review and again
   after successful check publication, immediately before Ready. After GitHub
   reports the PR Ready, the conductor fetches the PR and repeats SHA, authority,
   root proof, mergeability, required CI, thread, and claim checks before it
   renews the lease. After that renewal it performs one last contiguous sweep of
   surface claims, App-provenance check runs, required CI, review-check status,
   review threads, and (for root/control) the latest label event plus exact-SHA
   root-App proof. It then reads the full open-PR snapshot once more and locally
   revalidates its check rollup, target head/base SHA, queue intent, hold/root
   labels, repository/base boundary, observable Ready state, and mergeability
   while checking every competing arm. There is no intervening network call
   between that final snapshot and the separate arm operation.

If a PR is still armed but needs a fresh review (for example after GitHub's
native branch refresh), the conductor first publishes an in-progress check,
disables auto-merge, restores draft state, and verifies that parking is visible.
It publishes success only after the model quorum and every post-review gate pass
while the shared lease is freshly renewed. The PR is still an unarmed draft at
that moment, so success cannot itself merge it. A final mutable-state sweep then
precedes Ready, and another sweep follows the observable Ready transition before
the separately leased arm call.

A queued PR that a maintainer manually marked Ready but did not arm is returned
to draft before any model call and deferred for one cycle. That visible parking
step is intentional: a crash or lease loss between Ready and arm cannot waste a
full two-model review or publish success while a PR is outside the expected
draft state. The next cycle performs a fresh review from the observable draft.

Queue intent, root authorization, and `merge:hold` are hard conductor controls
through that final pre-arm sweep. Once GitHub accepts native auto-merge, its
required checks and branch-protection transaction are the merge-point authority;
GitHub exposes no atomic label/intent predicate at that point. A later conductor
cycle still attempts to revoke the check, disable auto-merge, and restore draft
when it observes lost authority or a hold, but this is best-effort post-arm
recovery rather than a guaranteed merge-point veto. An immediate operational
stop must disable auto-merge (or disable execution and the armed request), not
rely solely on adding a label.

Surface claims are a cooperative authoring collision signal rather than a
GitHub merge primitive. The final claim read is immediately before the final
GitHub sweep, but a new claim can still arrive after it. That residual race can
create rebase work; it cannot bypass the required App checks, CI, resolved-
conversation rule, or branch-protection transaction. Authors must therefore
claim before editing, not wait until a PR is already being armed.

If any post-review gate changes after `agent-review` was marked successful, the
conductor immediately publishes a newer in-progress check on that SHA, disables
auto-merge, and converts a Ready PR back to draft. The same recoverable
invalidation applies when an already-armed PR later gains `merge:hold`, loses
queue/root authority, opens a review thread, collides with an active surface
claim, or observes a failed/unreadable mutable gate. Once the gate recovers, the
same SHA receives a fresh model review; it can never reuse the revoked approval.
The one deliberate exception is the period while GitHub reports an armed PR as
`BEHIND`: strict branch protection prevents that head from merging, and GitHub's
native refresh creates a new SHA where `agent-review` is absent. Pending CI on
that refreshed SHA parks the PR recoverably until CI passes and review reruns.

Only a substantive model denial or deterministic policy escalation publishes
`failure`. Those terminal states also add `merge:escalate`, post a visible
comment, and are skipped by queue selection so they cannot starve newer PRs.
A missing CLI, timeout, malformed provider envelope, or other reviewer
unavailability instead leaves a newer in-progress check and recoverable
`waiting` result with no escalation label. `--retry-review` is the explicit
operator override for a terminal result on an unchanged SHA after the operator
removes `merge:escalate`; use it only with `--pr N` so the target is unambiguous.

Revocation publishes the replacement check first, then attempts disable-auto
and draft restoration independently. One failed GitHub write cannot suppress
the other safety attempts; any partial revocation is reported as an invariant
error. Post-review API/read failures follow the recoverable path, including PR
refresh, root proof, review-status, queue, Ready, and arm operations.

Approval is a GitHub check run named `agent-review` on the reviewed SHA. Every
execute cycle verifies that branch protection is strict, applies to
administrators, requires `agent-review` from exactly
`UNITARES_MERGE_REVIEW_APP_ID`, retains at least one non-review CI requirement,
requires all review conversations to be resolved, and that repository
auto-merge plus squash merging are enabled. When root automation is configured,
it additionally requires `agent-root-approval` from exactly the root App ID;
ordinary heads receive that App's neutral/successful not-applicable result. Gate install
and removal enforce the same non-review-CI/strict/admin invariants before and
after mutation. Legacy commit statuses, a same-named check from another App, or
an unbound context never count. If GitHub refreshes the branch, the new SHA has
no approval and cannot merge until both model families review it again.

## Risk policy

| Tier | Examples | Gate |
|---|---|---|
| Low | explicitly recognized ordinary documentation | both model families; opposite branch-prefix family first |
| Medium | tests, Python/BEAM/dashboard/runtime, scripts/skills, and every unclassified path | two reviews |
| Root/control | migrations, identity/auth/authorization/OAuth/JWT/token/credential/session authority, GitHub workflows, repository test/CI configuration, dependency/container manifests, releases, branch protection, delivery contract, conductor and its tests, and transitive merge/claim-serialization lease-plane implementations/clients/deployment | verified root-App label plus exact-SHA `agent-root-approval` check, then two reviews; otherwise paused root maintenance window |

Dependency root matching is basename/full-match based rather than substring
based. It covers Python, Node, Rust, Go, JVM, .NET, Ruby, PHP, Elixir/Erlang,
Swift, Dart, C/C++, Bazel/Nix/Terraform/Helm manifests and locks, plus
`Dockerfile`/`Containerfile` and Compose variants in nested directories.
Lookalikes such as `requirements_parser.py` or `package-lock.json.backup` do not
inherit root authority merely from a substring.

The root path list lives in code and is unit-tested. Adding or weakening an
exception modifies the conductor itself, so that change is recursively a root
decision. The segment heuristic intentionally classifies broad names such as
`auth`, `identity`, `lease`, `claim`, `session`, `token`, and `security` as root
authority. Expect false-positive manual escalations during the soak; narrowing
one is itself a root/control change and must be justified with boundary tests.

## Operating modes

Classification-only shadow is the default and makes no GitHub changes or model
calls:

```bash
python3 scripts/ops/merge_conductor.py --json
```

It may update the user-private armed-stall observation file under
`~/.cache/unitares/`; report-only refers to GitHub/model side effects, not that
local operational timestamp.

One-shot report-only review invokes models but does not comment, publish a check,
ready, or merge:

```bash
python3 scripts/ops/merge_conductor.py --pr 123 --review --json
```

Execution can be requested explicitly after rollout:

```bash
UNITARES_MERGE_CONDUCTOR_EXECUTE=1 \
  python3 scripts/ops/merge_conductor.py --json
```

That command succeeds only inside the isolated service boundary. Root creates
the fixed `/etc/unitares/merge-service-boundary.json` manifest (mode `0644`,
root-owned, not group/world writable), using the real numeric UIDs and absolute
deployment paths:

```json
{
  "version": 3,
  "service_uid": 502,
  "reviewer_uid": 503,
  "author_uids": [501],
  "review_key_path": "/var/db/unitares-merge-credentials/review-app.pem",
  "code_root": "/opt/unitares-merge",
  "service_home": "/var/db/unitares-merge",
  "reviewer_home": "/var/db/unitares-merge-reviewer",
  "credential_root": "/var/db/unitares-merge-credentials",
  "review_runner_path": "/opt/unitares-merge/scripts/ops/merge_review_worker.py",
  "python_executable_path": "/opt/unitares-merge-python/bin/python3",
  "python_import_roots": [
    "/opt/unitares-merge-python/lib/python3.14",
    "/opt/unitares-merge-python/lib/python3.14/lib-dynload",
    "/opt/unitares-merge-venv/lib/python3.14/site-packages"
  ],
  "reviewer_python_path": "/usr/bin/python3",
  "claude_cli_path": "/opt/unitares-merge-review-bin/claude",
  "codex_cli_path": "/opt/unitares-merge-review-bin/codex",
  "reviewer_path": [
    "/opt/unitares-merge-review-bin",
    "/usr/bin",
    "/bin"
  ],
  "github_cli_path": "/opt/unitares-merge-bin/gh",
  "conductor_path": [
    "/opt/unitares-merge-bin",
    "/usr/bin",
    "/bin"
  ],
  "surface_repo": "cirwel/unitares",
  "surface_claim_registries": [
    {
      "author_uid": 501,
      "path": "/Users/cirwel/.local/state/git-surfaces"
    }
  ],
  "secrets_env_path": "/var/db/unitares-merge-credentials/secrets.env"
}
```

The conductor HOME and credential root must be service-owned mode `0700`; the
key and secrets file are service-owned regular files at mode `0600`. The
reviewer HOME is reviewer-UID-owned mode `0700` and contains only Claude/Codex
subscription state. Neither credential file may be inside the conductor HOME,
reviewer HOME, or deploy tree. The entire deploy tree—including the worker—must
be root-owned, non-shared-writable, and free of escaping symlinks. The manifest
lists the exact conductor interpreter and complete module-search roots,
including stdlib, native extensions, and site-packages. The wrapper first
validates those trees with root-owned `/usr/bin/python3 -I -S`, before the
configured interpreter can execute a `.pth` file; the conductor then starts
with `-I -S`, repeats the attestation, and only then adds the declared roots to
`sys.path`. Provider CLI files, reviewer Python, and every
ancestor/runtime-PATH directory must likewise be root-owned and
non-shared-writable. If a client uses
`#!/usr/bin/env node`, deploy the pinned Node runtime in the first attested PATH
directory. The conductor UID may read but cannot replace any code later executed
as the reviewer UID. Mutable JSONL/lock/stall state must live in the conductor
HOME, not this deploy tree; the LaunchDaemon template sets the JSONL path there.
The manifest separately pins the absolute GitHub CLI and every conductor PATH
directory. The conductor invokes that resolved executable directly and replaces
the subprocess PATH with the attested list before exposing either the service
PAT or review-App token. Copy `gh` into `/opt/unitares-merge-bin` as root; do not
point this field or the LaunchDaemon PATH at Homebrew or another author-writable
package prefix.

Each `surface_claim_registries` entry is the real author UID's state root (the
directory containing `claims/`), not the deploy checkout. Version 3 binds it to
the passwd account's canonical `~/.local/state/git-surfaces` directory as well
as its UID; an attested but empty lookalike directory is rejected. Custom
`GIT_SURFACE_STATE_DIR` or `XDG_STATE_HOME` layouts are intentionally unsupported
in execute mode until a later root-manifest version can attest their source.
The directory remains author-owned so `git surface claim/release` works, must
not be group/world writable, and must be readable/traversable by the conductor
through a narrowly provisioned group or ACL. There must be exactly one entry
for every `author_uids` value. Active records are matched by `surface_repo`, not
by a worktree path, which covers all linked worktrees and avoids Git's
foreign-owner `safe.directory` behavior.

Root provisions a second unprivileged account (for example
`unitares-merge-reviewer`) with the manifest's reviewer UID/HOME and a narrow
sudoers rule whose only permitted run-as command is the root-deployed worker:

```sudoers
Cmnd_Alias UNITARES_REVIEW_WORKER = /usr/bin/python3 ^-I -S /opt/unitares-merge/scripts/ops/merge_review_worker[.]py (--probe|--probe --deny-read (/var/db/unitares-merge-credentials|/var/db/unitares-merge-credentials/review-app[.]pem|/var/db/unitares-merge-credentials/secrets[.]env)|--preflight|--review (claude|codex) --model [A-Za-z0-9][A-Za-z0-9._:/-]{0,127} --timeout [0-9]+([.][0-9]+)?)$
unitares-merge ALL=(unitares-merge-reviewer) NOPASSWD: UNITARES_REVIEW_WORKER
```

This anchored argument regex requires sudo 1.9.10 or newer (the rollout host
was verified on 1.9.17p2); validate the installed fragment with `visudo -cf`
before use. Substitute the manifest's exact three credential paths if the
example layout changes. The worker independently enforces the same grammar and
accepts `--deny-read` only for those root-attested paths, so the sudo entrypoint
cannot become a general reviewer-UID permission oracle.

The worker accepts only probe, fixed-CLI preflight, and Claude/Codex review
modes; it has no arbitrary command/path execution mode. It re-reads the fixed
root-owned manifest after crossing the UID boundary and derives its own
runner/CLI/PATH bindings rather than accepting them from conductor argv.
The conductor supplies only the root-attested `/usr/bin/python3 -I -S` and
worker path at that sudo boundary; the worker verifies the isolated/no-site
flags and interpreter again. `sudo -n -H` supplies the reviewer account's HOME
deterministically. Execute
startup verifies the run-as UID/HOME and negative credential reads before
GitHub access. The root-owned LaunchDaemon still runs as the conductor account.
Provision provider subscription logins only in the reviewer HOME. A same-user
manual invocation or ambient `HOME` change cannot satisfy this manifest.

Classification-only shadow does not need a review App ID, but when it runs as
the isolated service UID it does require the service PAT and exact profile
below; an author-run local report may continue using that author's ambient
`gh` session. Report-only model review and execute modes need
`UNITARES_MERGE_REVIEW_APP_ID` so approval reads can be filtered to one producer.
Execute mode additionally requires
`UNITARES_MERGE_REVIEW_APP_INSTALLATION_ID` and an absolute
`UNITARES_MERGE_REVIEW_APP_PRIVATE_KEY_PATH`; the optional
`UNITARES_MERGE_REVIEW_APP_CLIENT_ID` is the preferred JWT issuer and otherwise
the numeric App ID is used. The key must be a regular file owned by the isolated
service user with mode `0600`. The App installation needs repository
metadata read access and Checks write access only.

Isolated service shadow and execute mode require
`UNITARES_MERGE_SERVICE_GH_TOKEN` and
`UNITARES_MERGE_SERVICE_GH_CREDENTIAL_PROFILE` set to
`fine-grained-pat:administration-read,pull-requests-write,contents-read,checks-read,commit-statuses-read`.
The final permission is required because the CI rollup reads both check runs and
legacy commit-status contexts; a credential without it fails closed during the
provisioning canary rather than silently omitting a required status.
Execute mode additionally requires `LEASE_PLANE_BEARER_TOKEN`.
The service token is injected into each ordinary `gh` subprocess after every
ambient GitHub credential variable is removed; review check writes receive only
the separately minted review-App token. The service wrapper
loads these variables from
`${UNITARES_SECRETS_ENV:-~/.config/cirwel/secrets.env}` rather than embedding a
bearer or private key in the plist. Explicit plist/manual execution and review
flags take precedence over same-named secrets-file values, so the template's
`0` cannot be silently activated by a stale secrets file. Before sourcing, the
wrapper refuses a symlink, foreign owner, or any mode other than `0600`. The
template also pins `UNITARES_MERGE_PYTHON` to the absolute manifest interpreter
and always invokes it with `-I -S`; lazy dependency failures are caught and
written as structured JSONL errors. A JSONL-path failure itself cannot be
written to that path, so it replaces the cycle result with a structured `error`
on stdout/stderr for the LaunchDaemon log and alert.
`UNITARES_MERGE_LEASE_TTL_S` defaults to the
lease-plane maximum of 3600 seconds and rejects values below 1200 seconds. That
floor covers two sequential reviews at the default 420-second timeout plus a
360-second control-plane margin. If `UNITARES_MERGE_REVIEW_TIMEOUT_S` is raised,
the lease TTL must remain at least twice that timeout plus 360 seconds; values
that cannot satisfy the 3600-second lease-plane maximum are rejected before the
execute cycle acquires a lease. Expiry remains a crash-recovery bound, not
assumed ownership, because review start, each model call, approval, Ready, and
arm renew the exact lease ID. Report-only classification and review do not
acquire the global lease or mint an installation token.

Removing the conductor's required context is an explicit OS-root-only rollback.
The ordinary author or conductor UID is rejected before GitHub access:

```bash
sudo -H --preserve-env=GH_TOKEN,UNITARES_MERGE_REVIEW_APP_ID \
  /opt/unitares-merge-python/bin/python3 -I -S \
  /opt/unitares-merge/scripts/ops/merge_conductor.py \
  --execute --uninstall-gate --branch master --no-log \
  --lock /var/run/unitares-merge-conductor-setup.lock
```

This patches the App-bound check list to remove only `agent-review`; every
other required `(context, App ID)` identity remains intact. Removal refuses to
proceed unless strict updates and administrator enforcement are true, retains at
least one non-review required check, and re-verifies those protection invariants
after the patch. Installation and label setup carry the same local OS-root
preflight. This protects the conductor command path; it cannot stop a holder of
an admin-scoped GitHub token from patching branch protection directly. The
operator credential is the actual remote authority and must be unavailable to
author/conductor/reviewer UIDs except during the declared root window.
GitHub commands have a 60-second timeout by default so a stalled CLI cannot
hold the conductor's process lock indefinitely. Override it with
`UNITARES_MERGE_GH_TIMEOUT_S` only for a diagnosed slow API path.
The legacy local-author shadow may invoke `git surface list`; the isolated
service instead scans the attested registries directly with a 10,000-record,
64-KB-per-record, and fixed 30-second bound. This avoids Git safe-directory/PATH
dependence and fails closed rather than holding the global merge lease until
TTL.

The five-minute isolated LaunchDaemon reads the same environment flags and its
template ships with both set to `0`. An old same-user LaunchAgent has neither
variable and therefore remains report-only; boot it out before daemon activation.

`UNITARES_MERGE_ARMED_STALL_S` changes the native-update fallback threshold
(default 900 seconds). Lock and stall state live under `~/.cache/unitares/` by
default and are mode `0600`.

Revalidate dated infrastructure claims with the live `gh api` branch-protection
and repository endpoints during every activation or root maintenance window;
the dates here are evidence snapshots, not timeless configuration promises.

## Rollout

This order is load-bearing:

1. Land the conductor bootstrap through the existing root/human gate. The gate
   may not authorize its own installation.
   This one bootstrap patch necessarily combines the conductor, its adversarial
   tests, delivery contract, and dormant rollout instructions, so its churn is
   above the conductor's own 8,000-line evidence envelope. That is a documented
   bootstrap condition, not an executable bypass: the script has no bootstrap
   exemption flag, this PR remains unqueued and draft, and every later over-limit
   PR is non-approvable and escalates through the ordinary root/human path.
   Existing-PR queue delivery depends on the `merge:auto` bootstrap label from
   step 4. Until that step completes, `ship.sh` leaves such a PR unqueued,
   prints the explicit reason and URL, and does not mistake commit/push success
   for queue success. New PRs remain queueable through their creation-time body
   marker.
2. Deploy the new script and compatibility entrypoint to the historical
   LaunchAgent, leaving it report-only. This retires the redundant polling
   updater while preserving shadow visibility. As root, create the dedicated
   unprivileged conductor and reviewer accounts, separate mode-`0700` HOMEs,
   disjoint mode-`0700` conductor credential root, root-owned non-writable deploy
   tree, root-owned interpreter/stdlib/native-extension/site-packages roots,
   root-deployed worker, pinned provider binaries/runtimes, narrow sudoers rule,
   mode-`0600` secrets/key files, log directory, pinned root-owned GitHub CLI,
   root-owned version-3 boundary manifest, and rendered LaunchDaemon with
   execution still `0`. Give the
   conductor read/traverse access—but never write access—to every attested
   author's canonical `~/.local/state/git-surfaces` registry. Ensure neither
   `GIT_SURFACE_STATE_DIR` nor a non-default `XDG_STATE_HOME` is used. Confirm
   the wrapper's OS-Python preflight and
   configured `-I -S` interpreter import `cryptography` and `src.lease_plane`,
   reject an author-writable `.pth`/site-packages canary, reach the shared lease
   plane, and observe the reviewer UID/HOME plus denied credential reads. A
   forced missing-dependency probe must produce an audited `error` row.
3. Create a dedicated fine-grained PAT restricted to `cirwel/unitares`, with
   Administration read, Pull requests write, Contents read, Checks read, and
   Commit statuses read—no Administration write, Contents write, Actions, or
   Workflows. Store it only
   as `UNITARES_MERGE_SERVICE_GH_TOKEN` in the conductor secrets file with the
   exact profile string documented above. Preserve the token-configuration
   permission record as rollout evidence, confirm branch-protection read and PR
   Ready/draft/label/comment/SHA-bound update/auto-merge operations on a
   disposable canary, and confirm the service has no ambient `gh` login/admin
   token. The update canary must use the REST pull-request update endpoint and
   prove a deliberately stale `expected_head_sha` is rejected.

   From the conductor account, use `/opt/unitares-merge-bin/gh api --include`
   with `X-GitHub-Api-Version: 2026-03-10` to read both
   `repos/cirwel/unitares/commits/$CANARY_SHA/check-runs` and
   `repos/cirwel/unitares/commits/$CANARY_SHA/statuses?per_page=1`. Require HTTP
   200 for both, record the `X-Accepted-GitHub-Permissions` headers, and fail
   provisioning if the version or `commit_statuses=read` permission is not
   accepted. This canary must use the service PAT and pinned executable, not an
   ambient operator login.

   Create a dedicated review GitHub App, grant it repository Metadata read and
   Checks write, install it only on `cirwel/unitares`, and store its App ID,
   installation ID, optional client ID, and service-only private-key path under
   the environment names above. This App publishes review evidence only; keep
   it distinct from any root-approver App. Validate the real cross-process
   lease boundary from an environment that has the operator bearer:

   ```bash
   UNITARES_TEST_LIVE_MERGE_LEASE=1 \
     pytest -q tests/test_merge_conductor.py::test_live_global_merge_lease_has_exactly_one_cross_process_winner
   ```

   The test spawns two independent clients and requires exactly one acquisition
   and one `held_by_other` result on a unique shared surface. Also validate the
   installed provider clients' behavioral isolation:

   ```bash
   UNITARES_TEST_LIVE_REVIEWER_ISOLATION=1 \
     pytest -q tests/test_merge_conductor.py::test_live_reviewer_isolation_blocks_patch_directed_effects
   ```

   From an authoring-agent login, run the credential-boundary canary:

   ```bash
   UNITARES_TEST_MERGE_SERVICE_BOUNDARY=1 \
     pytest -q tests/test_merge_conductor.py::test_live_author_cannot_read_merge_service_credentials
   ```

   It must prove the current author UID is listed in the root manifest, cannot
   read the review key, secrets file, conductor HOME, or reviewer HOME, and
   cannot write the deploy root. From the conductor account, run the root-
   attested worker preflight and a report-only review; startup must prove the
   reviewer UID also cannot open the credential root, key, or secrets. Any
   root-approver key must live behind a different service/host boundary and is
   never an input to either merge service.

   From the conductor account with its normal secrets environment, exercise the
   real sudoers boundary and pinned CLIs:

   ```bash
   UNITARES_TEST_LIVE_REVIEWER_WORKER=1 \
     pytest -q tests/test_merge_conductor.py::test_live_reviewer_worker_is_separate_and_cannot_read_conductor_credentials
   ```

   Record the exact Claude/Codex version strings with the canary. Any later
   mismatch is a rollout stop until a root change updates the pins and reruns it.
   Page on that specific preflight error, and measure the complete root-owned
   deploy/import-tree attestation duration during the soak so a slow stat walk
   is distinguishable from provider or lease unavailability.

   Before leaving provisioning, create a short-lived `git surface claim` from
   each author UID/worktree registry and run classification shadow as the
   conductor UID. It must report that exact repository claim without Git
   `safe.directory` configuration; after release, it must report clear. An
   unreadable registry must produce `waiting`, never a false clear.
4. Create/update the four labels:

   ```bash
   sudo -H --preserve-env=GH_TOKEN,UNITARES_MERGE_REVIEW_APP_ID \
     /opt/unitares-merge-python/bin/python3 -I -S \
     /opt/unitares-merge/scripts/ops/merge_conductor.py \
     --execute --install-labels --no-log \
     --lock /var/run/unitares-merge-conductor-setup.lock
   ```

   Complete this step before expecting default `ship.sh` runs to queue an
   already-existing PR. The script deliberately refuses a body read/modify/write
   fallback when the set-valued label is absent.

   Root/control PRs remain manual unless a separate approval service is
   installed and its GitHub App ID is set in
   `UNITARES_MERGE_ROOT_APPROVER_APP_ID`.

5. Boot out the historical same-user LaunchAgent and bootstrap the prepared
   isolated system LaunchDaemon with execution still `0`. Run its
   classification-only shadow for at least 24 hours. Compare every
   `would_review`, wait, hold, and escalation with the actual PR state, and
   alert if two consecutive five-minute records are missing or one armed PR
   remains unmerged with the same SHA/state for 15 minutes, including
   non-`BEHIND` waits. This service soak, not a same-user author process, is the
   availability evidence for the future required check.
6. Run a report-only model review on at least one low-risk Codex PR and one
   low-risk Claude PR if both are available. Each run must show both reviewer
   families, with branch prefix changing order only.
7. Migrate or finish existing open PRs. Installing the check gate while
   unqueued PRs remain would intentionally block them.
8. In a declared root maintenance window, keep resident execution at `0` and
   first enable “require conversation resolution before merging” in branch
   protection, then install the SHA- and App-bound required review check:

   ```bash
   sudo -H --preserve-env=GH_TOKEN,UNITARES_MERGE_REVIEW_APP_ID \
     /opt/unitares-merge-python/bin/python3 -I -S \
     /opt/unitares-merge/scripts/ops/merge_conductor.py \
     --execute --install-gate --branch master --no-log \
     --lock /var/run/unitares-merge-conductor-setup.lock
   ```

   Re-read branch protection and confirm `agent-review` has the exact configured
   App ID, required conversation resolution remains enabled, and every
   pre-existing required check retains its App identity. If root automation is
   being provisioned, first add `agent-root-approval` as a required check pinned
   to the distinct root App and prove that service publishes neutral/success for
   ordinary heads and success only after explicit root authorization for root
   heads. Run
   one execute-mode no-op/empty-queue cycle under the service UID and confirm
   the JSONL record is neither `error` nor permanently `busy`; this proves the
   repository-global lease and App credentials are usable from the real
   boundary.
9. Rehearse required-check outage recovery before activation. With resident
   execution still `0`, use a disposable unqueued draft to confirm the absent
   `agent-review` check blocks it. Time a root operator running
   the OS-root-only `--execute --uninstall-gate`, re-read protection to prove
   only the conductor context was removed, and confirm all pre-existing checks
   retain App identity.
   Restore the daemon, reinstall the gate, and re-read it again. The operational
   target is alert after two missed intervals and removal of the stranded
   requirement within 15 minutes when service recovery cannot meet that bound.
   Record the measured drill rather than assuming the command is available.
10. Set `UNITARES_MERGE_CONDUCTOR_EXECUTE=1` on the isolated LaunchDaemon and
    reload it. Leave `UNITARES_MERGE_CONDUCTOR_REVIEW=0`; execute mode performs
    reviews automatically. Canary one low-risk PR. Confirm both model verdicts,
    the check run's App ID and exact SHA, the comment, Ready transition, the
    post-Ready and final-boundary mutable-state reads, native auto-merge,
    post-merge CI, and
    deployment health before queuing more. Exercise a canary that adds
    `merge:hold` during Ready and confirm it is parked without an arm request.
    Then remove the hold, let the same SHA receive a fresh two-model review and
    newer successful `agent-review` run, and confirm GitHub accepts and merges
    it; this validates the load-bearing newest-check-run recovery behavior
    before broader activation.
    Repeat with a final-boundary failing required check, newly opened review
    thread, active surface claim, and (when configured) newer failed root-App
    check while the root label remains; every case must park without arming.

Do not install the required check before the conductor and App credential are
deployed and able to write it. Do not enable execution before the App-bound
requirement exists or before the root-attested OS boundary and negative author
and reviewer credential canaries pass.

Once required, `agent-review` is intentionally a single fail-closed merge gate:
a stopped daemon, expired provider login, version-pin failure, lease-plane
outage, or App outage halts every merge, including administrator merges because
`enforce_admins=true`. The root operator owns availability and the rehearsed
`--uninstall-gate` recovery. Do not accept that coupling without the service
soak, missed-cycle alert, credential canaries, and timed removal/reinstall drill
above.

### Root maintenance without an approver App

The live `master` protection was verified on 2026-08-17 with `strict=true` and
`enforce_admins=true`. Consequently, “merge manually” cannot mean bypassing a
missing `agent-review` check. For a root/control PR when no independently
authenticated root-approver App is configured:

1. set resident execution to `0`, reload it, and verify no conductor cycle is
   running;
2. as OS root, run the `sudo -H` command above with
   `--execute --uninstall-gate --branch master --no-log --lock /var/run/unitares-merge-conductor-setup.lock` to
   remove only `agent-review`;
3. perform the human/root review and merge through every remaining protected
   check;
4. deploy the merged control-plane revision and run report-only classification
   plus the relevant model canary;
5. as OS root, run the `sudo -H` form with
   `--execute --install-gate --branch master --no-log --lock /var/run/unitares-merge-conductor-setup.lock`, verify
   the context is required, canary a low-risk PR, and only then re-enable
   execution.

Pause normal autonomous merging throughout this window. The install preflight
also refuses an armed or unmanaged open-PR state, so the gate cannot be restored
over an ambiguous queue. This is the recovery/upgrade path for the conductor
itself; the preferred no-pause path is the separately credentialed root App.

## Rollback and recovery

Fleet stop:

1. Set `UNITARES_MERGE_CONDUCTOR_EXECUTE=0` and reload the isolated LaunchDaemon.
2. As OS root, remove the required conductor context with
   `--execute --uninstall-gate` as shown above, so human/manual PRs are not
   stranded while check production is off.
3. Disable any outstanding request with `gh pr merge --disable-auto <n>`.
4. Add `merge:hold` to PRs that must remain parked. In execute mode the next
   conductor cycle also disarms an armed PR carrying that label; for an
   immediate stop, run the explicit `gh pr merge --disable-auto` command because
   a post-arm label is not an atomic merge-point veto.

If an armed PR remains `BEHIND` after both GitHub's native updater and the
guarded fallback, disable auto-merge, inspect the conflict/check state, run one
manual `gh pr update-branch <n>` only when mergeable, and requeue it after the
fresh CI head is visible. The JSONL log records the first-wait duration and the
fallback request.

An armed PR in any other unchanged wait state is still the sole serial-train
target and can starve peers. The rollout monitor must page at 15 minutes. The
operator then sets execution to `0`, disables that exact PR's auto-merge request,
adds `merge:hold`, restores draft, diagnoses the missing check/thread/GitHub
state, and re-enables the resident only after the queue contains no ambiguous
armed request. The conductor does not silently skip an already-authorized arm.

If the conductor is killed while holding `maintenance:/merge_train/...`, its
remote-holder lease expires within `UNITARES_MERGE_LEASE_TTL_S` (at most one
hour). Prefer waiting for that bounded recovery. If waiting is unacceptable,
first set execution to `0`, verify no conductor PID is alive, and query
`lease_plane.surface_leases` for the one unreleased surface matching
`maintenance:/merge_train/%`. Copy that exact lease UUID—never use a bulk
pattern—and force-release it with the separately protected
`LEASE_FORCE_RELEASE_TOKEN` and `ForceReleaseRequest` path documented in
[the lease-plane operator runbook](lease-plane-operator-runbook.md#lease_force_release_token--provisioning-and-rotation-rfc-710).
Record the forced release in the incident/audit log before re-enabling the
resident.

A failed review is bound to its head SHA. Correct the patch and push a new
commit to obtain a fresh review. `--retry-review` exists for a confirmed
transient reviewer failure on the unchanged SHA; remove `merge:escalate` first
and target the PR explicitly. It must not be used to shop for a more favorable
answer after a substantive disagreement. Provider unavailability is already
recoverable and does not need this override.

The audit trail consists of:

- the configured JSONL log for each cycle (`$SERVICE_HOME/merge-conductor.jsonl`
  in the isolated daemon; `data/logs/merge-conductor.jsonl` in a development
  checkout by default);
- the SHA- and App-bound GitHub check run;
- a structured PR comment containing reviewer/model provenance and findings;
- GitHub's Ready, auto-merge, and merge events.

## Native merge queue

GitHub's merge queue remains the preferable long-term scheduler, but GitHub
currently offers it only for organization-owned repositories. `cirwel/unitares`
is user-owned, so the conductor provides the missing serial queue. If the repo
moves to an organization, retain the independent `agent-review` gate and replace
the local serialization/update logic with the native queue after a canary.
