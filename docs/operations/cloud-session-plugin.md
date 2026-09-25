# Governance Plugin in Claude Code Cloud Sessions

**Status:** Runbook (v0, 2026-09-21)
**Audience:** Operators running `unitares` work in Claude Code cloud sessions
(claude.ai/code, the mobile Code tab, `claude --cloud`, routines) who expect the
same governance lifecycle they get locally.

A cloud session starts with governance *tools* and no governance *hooks*: the
MCP surface is present, auto-onboard and the per-turn check-in are not. This
runbook says why, what it costs, and how to close the gap.

## Why the plugin is absent by default

Two independent causes. Fixing one does not fix the other.

### 1. Install state does not travel

`/plugin marketplace add` writes to `~/.claude/plugins/known_marketplaces.json`
on the machine that ran it — per user, not per project. A cloud container is
provisioned fresh, so it starts with no `known_marketplaces.json`, no `cache/`,
and no `marketplaces/`. `/plugin` itself is a terminal-only command and is not
available inside a cloud session, so it cannot be repaired from within one.

The documented fallback — declaring `extraKnownMarketplaces` and
`enabledPlugins` in the repository's `.claude/settings.json` — is deliberately
closed in this repo. `.gitignore` ignores `.claude/`, and
`scripts/dev/check-repo-scope.sh` hard-fails both a `!.claude` re-include
(Rule 0) and any tracked `.claude/` file (Rule 1). That guard is doing its job;
it happens to also close the door cloud sessions use.

The account-level sync channel (`~/.claude/plugins/synced/`) exists and works —
the sibling `~/.claude/skills/synced/` arrives populated — but it carries only
plugins enabled on the claude.ai account, and this plugin is not published to a
catalog the account subscribes to.

### 2. Container egress is allowlisted

This is the cause that survives installing the plugin, and the one most likely
to be misread as "the plugin is broken."

The MCP tools work in a cloud session because a remote MCP connector is proxied
server-side through Anthropic's infrastructure; the traffic never leaves the
sandbox. Hook scripts are different: they make outbound HTTP *from* the
sandbox, through the session's egress proxy, which refuses any domain outside
the environment's network allowlist unless an environment API credential
explicitly covers that host:

```
$ curl https://example.com
curl: (56) CONNECT tunnel failed, response 403
$ curl -o /dev/null -w '%{http_code}' https://api.github.com
200
```

Both `http://` and `https://` are intercepted, and a raw TCP connection to a
non-standard port (8767, 8788) does not complete. So a governance server
reachable from a cloud session must be:

- served over 443 at a hostname covered by an environment API credential or
  added to the cloud environment's network allowlist, **not** a bare
  `host:8767`, and
- addressed with an `https://` `UNITARES_SERVER_URL`.

Without one of those reachability paths, hooks install and run but every
network path reports OFFLINE.

## Hook audit — residentless cloud container

All 13 hooks `hooks/claude-hooks.json` wires were executed in a cloud container
with no governance server, no lease plane, no resident roster, and the default
`UNITARES_FILE_LEASES_REQUIRED=0`. Every one exited 0. None blocked a tool call.
None errored. Setting `UNITARES_FILE_LEASES_REQUIRED` to a truthy value changes
that posture deliberately: `pre-edit` then fails closed when leases are absent.

| Hook | Event | Verdict without a server | Cost |
| --- | --- | --- | --- |
| `session-start` | SessionStart | Degrades to an `OFFLINE` banner and still injects the skill pointers | 0.20s |
| `watcher-context` | SessionStart, UserPromptSubmit | Inert — `UNITARES_WATCHER_ENABLED` defaults to `0` and the agent path defaults empty | ~0s |
| `user-prompt-submit` | UserPromptSubmit | Silent no-op | 0.09s |
| `pre-edit` | PreToolUse Edit/Write | Fails open at the default `UNITARES_FILE_LEASES_REQUIRED=0`; fail-closed when that override is truthy | 0.12s |
| `post-edit` | PostToolUse | Silent no-op | 0.26s |
| `post-edit-release` | PostToolUse, failures, denials | Silent no-op | 0.11s |
| `post-edit-batch-release` | PostToolBatch | Silent no-op | 0.11s |
| `pre-governance-call` | PreToolUse on governance tools | Silent no-op | 0.12s |
| `post-checkin` | PostToolUse on check-ins | Silent no-op | 0.04s |
| `post-identity` | PostToolUse on onboarding | Silent no-op | 0.07s |
| `pre-push` | PreToolUse Bash | Queries PR state through `gh` where the image has it (not every hosted image does); fails open if the lookup fails or `gh` is absent | 0.06s |
| `post-stop` | Stop | Attempts a check-in, gives up | 0.37s |
| `session-end` | SessionEnd | Local cleanup only | 0.13s |

Pointed at a reachable-looking but proxy-blocked domain, `session-start` takes
0.45s and `post-stop` 0.95s — the 403 arrives fast, so no hook approaches its
declared timeout.

Two things that could have blocked portability and do not:

- **Runtime.** Every hook is bash plus stdlib Python. The only third-party
  import in the tree is `yaml`, confined to two skill-freshness helpers, and it
  is present in the container. The package declares `requires-python >= 3.12`
  while the container's default `python3` is 3.11; everything under `scripts/`
  and `src/` compiles clean on 3.11, and 3.12 and 3.13 are both installed.
- **Paths.** No machine-local absolute path anywhere in `hooks/`, `scripts/`,
  or `src/`. Every endpoint is an env-overridable default.

What is lost while OFFLINE is therefore exactly the network-dependent
behavior — auto-onboard, the per-turn substrate check-in, KG recall, file
leases — and nothing else. Hook presence alone still buys the SessionStart
skill pointers and, where the image has `gh`, the merged-PR push guard. Not
every Anthropic-hosted image has it: this runbook's audit found a
proxy-authenticated `gh`, and a hosted session on 2026-09-25 had none, only
the GitHub connector. Without `gh` the guard allows every push, because
`merged_pr_guard.py` fails open by design. A self-hosted environment depends
on the tools in its runner image.

## Wiring

**1. Create a private environment dedicated to `cirwel/unitares`.** Do not
reuse it for another repository or make it organization-shared. Setup runs as
root, environment variables are readable by every session that uses the
environment, and the first successful setup is cached for later sessions.
Keeping this environment repo-specific is therefore a security boundary, not
just an organization preference.

**2. Set the setup script** on that environment (claude.ai → cloud
environments). Configure it only after this file has landed on `master`:

```bash
origin=$(git remote get-url origin 2>/dev/null || true)
if [ "${origin%.git}" != "https://github.com/cirwel/unitares" ]; then
  echo "This environment is reserved for cirwel/unitares; refusing setup." >&2
  exit 1
fi
setup=$(git show origin/master:scripts/dev/cloud-session-setup.sh) || exit 1
bash -s <<<"${setup}" || true
```

The remote check fails closed before any checkout code runs as root **while the
environment cache is being built**. It cannot police later reuse: cached
sessions skip setup entirely, retaining the installed plugin and every
environment credential. The dedicated-environment requirement in step 1 is
therefore the security boundary; this check is only a provisioning defense. If
the environment is ever attached to another repository, retire or rebuild it
and rotate its credentials.

Reading the script from canonical `origin/master` prevents a task branch from
replacing the setup payload. A missing canonical script fails provisioning
instead of creating an empty cached environment. The final `|| true` applies
only after those trust checks: a transient plugin-install failure leaves the
UNITARES session without hooks rather than blocking it. The setup exports
`CLAUDE_CODE_PLUGIN_PREFER_HTTPS=1` because GitHub `owner/repo` marketplace
sources otherwise clone over SSH, but hosted containers have no operator SSH
key. Marketplace registration and plugin installation each have a 120-second
cap, so both sequential network steps still leave margin inside the platform's
roughly five-minute setup-script limit. Fresh install measured at 3.4s; re-runs
short-circuit. If installation fails, change the setup field to force a cache
rebuild or wait for cache expiry.

**3. Configure hook authentication and environment variables.** A setup script's
exports die with its shell and never reach the agent process, so these must be
declared as environment variables, not exported in the script:

| Variable | Value | Why |
| --- | --- | --- |
| `UNITARES_SERVER_URL` | `https://<allowlisted-host>` | Loopback default is meaningless in a container; must be HTTPS on implicit port 443 (or explicit `:443`), with no trailing slash because hooks append their paths verbatim |
| `UNITARES_CLOUD_PROXY_AUTH` | `1` when an environment API credential supplies `Authorization` | Nonsecret signal that setup cannot test the credential injected only after Claude launches |
| `UNITARES_HTTP_API_TOKEN` | hosted bearer credential, only when proxy credentials are unavailable | Environment-visible fallback header; its value must be accepted by the server's strict bearer allowlist |
| `UNITARES_FILE_LEASES_ENABLED` | `0` | No lease plane in-container; avoid the otherwise harmless connection-refused probe |
| `UNITARES_FILE_LEASES_REQUIRED` | `0` | Required leases override `ENABLED=0` and block edits when the lease plane is absent |

On Pro and Max, store the bearer as an environment **API credential** scoped to
the governance hostname, using an `Authorization: Bearer` header. Omit
`UNITARES_HTTP_API_TOKEN` and set the nonsecret
`UNITARES_CLOUD_PROXY_AUTH=1`. The agent proxy injects that credential only
after Claude Code starts, so setup cannot test it and deliberately ends with an
`UNVERIFIED` warning. The hooks fail open and are not an authentication test:
`session-start` checks the public health route, while `post-stop` suppresses
network/authentication failures.

The public server must configure the same credential in
`UNITARES_MCP_BEARER_TOKENS` and must not override `UNITARES_REST_STRICT=0`.
That hosted posture disables the trusted-network bypass, including when a
Cloudflare tunnel forwards requests to server loopback. The health response
advertises both booleans; runtime verification refuses to claim authentication
unless `rest_strict` and `mcp_bearer_required` are both true.

As the first action after Claude starts, run the canonical payload in runtime
verification mode:

```bash
unitares_runtime_setup="$(
  git show origin/master:scripts/dev/cloud-session-setup.sh
)" &&
  test -n "$unitares_runtime_setup" &&
  bash -s -- --verify-runtime <<<"$unitares_runtime_setup"
```

This repeats the harmless health request and invalid tool request after the
proxy credential is available, and checks the server's reported auth posture.
Extraction failure or an empty payload stops the command before Bash can report
a false-successful no-op.
It is an endpoint/authentication diagnostic only: `claude plugin list` reports
configured state, not whether this Claude process loaded the hooks. A zero exit
therefore does **not** prove automatic hooks are active. The command never
installs or enables a plugin in the active process because Claude Code loads
plugin hooks when a session starts.

Before relying on automatic onboarding/check-ins, require both pieces of
evidence: the setup run installed/enabled the plugin before process launch, and
the plugin's SessionStart output/context appeared when the new session began.
If that launch evidence is absent—or the diagnostic reports the plugin missing
or disabled—repair setup and start another cloud session. A zero diagnostic
exit with `server tool route usable (authenticated validation response)` proves
only endpoint/authentication readiness; a wrong credential exits non-zero.

Team and Enterprise do not currently expose environment API credentials. If a
bearer environment variable is unavoidable, use only this private dedicated
environment and a narrowly scoped, revocable token. Never place that durable
secret in a shared environment.

**4. Make the server hostname reachable.** An environment API credential's
Allowed websites entry also grants network reachability to that host. When
using `UNITARES_HTTP_API_TOKEN` instead, add the hostname to the environment's
network allowlist. One of these paths is what lets governance become ONLINE.

Leaving both reachability paths undone is a legitimate choice. A cloud session
then runs with governance tools and OFFLINE hooks, which is a coherent posture:
reads and manual `sync_state` calls still work through the MCP connector, and
only the automatic lifecycle is absent.

## Review from hosted sessions

A third gap, in the same family and worth knowing before shipping cloud-session
work: the hosted image can read and update the PR, but it may not have the
tools `review.sh` needs.

- **`gh` is not guaranteed.** Where the image has the proxy-authenticated
  `gh` this runbook's audit found, `gh pr view`, `gh pr comment`,
  `review.sh record`, and the merged-PR push guard work for repositories
  attached to the session. The proxy supports the PR operations these paths
  use, although it restricts unrelated GraphQL operations. The hosted session
  of 2026-09-25 had no `gh`; its only PR channel was the GitHub connector.
  Check with `command -v gh` rather than assuming either.
- **Reviewer CLIs vary too.** Local review prefers the other model:
  `default_reviewer` picks `codex` for branches not named `codex/*`. If that
  CLI is absent or unavailable, `review.sh` tries the other provider in a
  fresh reviewer session. With `git config review.native true`, it can instead
  request and join native GitHub Codex review without needing a local Codex
  CLI. The 2026-09-25 session had the `claude` CLI and no `codex`.

Without `gh`, the session still completes its own review gate. Run the review
in a fresh context that did not write the diff: a fresh `claude -p` session
given only the diff and `REVIEW_PROMPT` from `review_gate.py`, a subagent, or
a council reviewer. Render the record with
`./scripts/dev/review.sh record <file> --independent --emit --reviewer-name <honest-name>`,
which needs no `gh`, and post the printed body verbatim through the connector.
The steps, and the operator decision that lets a same-session subagent review
complete the gate, are in
[Recording a review without gh](github-workflow-conventions.md#recording-a-review-without-gh).
#2425 was reviewed this way.

The author cannot substitute their own self-check for independent review.
`cmd_record` requires an explicit independence attestation; it does not infer
independence from a provider name or branch prefix. A separate reviewer using
the same model is valid, though it is the weakest reviewer the gate accepts,
so name it honestly. Consult advice alone is not a code review. If no reviewer
completes, the author reports the blocker; CI shows a neutral warning. The
author keeps the PR draft until review is complete.

The record is keyed on the diff, so it can be produced at any later point
without re-pushing.

## What this does not cover

The audit establishes that the hooks run and fail open in a residentless
container. It does not establish that the full lifecycle behaves correctly
against a *reachable* server from a cloud session — no such server was
available to test against. Onboarding, check-in submission, and KG recall over
the egress proxy remain unverified.
