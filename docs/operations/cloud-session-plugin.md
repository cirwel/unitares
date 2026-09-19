# Governance Plugin in Claude Code Cloud Sessions

**Status:** Runbook (v0, 2026-09-19)
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
the environment's network allowlist:

```
$ curl https://example.com
curl: (56) CONNECT tunnel failed, response 403
$ curl -o /dev/null -w '%{http_code}' https://api.github.com
200
```

Both `http://` and `https://` are intercepted, and a raw TCP connection to a
non-standard port (8767, 8788) does not complete. So a governance server
reachable from a cloud session must be:

- served over 443 at a hostname added to the cloud environment's network
  allowlist, **not** a bare `host:8767`, and
- addressed with an `https://` `UNITARES_SERVER_URL`.

Without that, hooks install and run but every network path reports OFFLINE.

## Hook audit — residentless cloud container

All 13 hooks `hooks/claude-hooks.json` wires were executed in a cloud container
with no governance server, no lease plane, and no resident roster. Every one
exited 0. None blocked a tool call. None errored.

| Hook | Event | Verdict without a server | Cost |
| --- | --- | --- | --- |
| `session-start` | SessionStart | Degrades to an `OFFLINE` banner and still injects the skill pointers | 0.20s |
| `watcher-context` | SessionStart, UserPromptSubmit | Inert — `UNITARES_WATCHER_ENABLED` defaults to `0` and the agent path defaults empty | ~0s |
| `user-prompt-submit` | UserPromptSubmit | Silent no-op | 0.09s |
| `pre-edit` | PreToolUse Edit/Write | Fails open; lease plane refused at loopback speed | 0.12s |
| `post-edit` | PostToolUse | Silent no-op | 0.26s |
| `post-edit-release` | PostToolUse, failures, denials | Silent no-op | 0.11s |
| `post-edit-batch-release` | PostToolBatch | Silent no-op | 0.11s |
| `pre-governance-call` | PreToolUse on governance tools | Silent no-op | 0.12s |
| `post-checkin` | PostToolUse on check-ins | Silent no-op | 0.04s |
| `post-identity` | PostToolUse on onboarding | Silent no-op | 0.07s |
| `pre-push` | PreToolUse Bash | Inert — `merged_pr_guard.py` fails open when `gh` is absent, and it is | 0.06s |
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
skill pointers and the merged-PR push guard (the latter only where `gh` exists).

## Wiring

**1. Set the setup script** on the cloud environment (claude.ai → cloud
environments). It runs after the repository is cloned:

```bash
bash scripts/dev/cloud-session-setup.sh
```

Fresh install measured at 3.4s; re-runs short-circuit. The script never fails
the session — a failed install leaves the session in the state it would have
had anyway.

**2. Set environment variables** on the same environment. A setup script's
exports die with its shell and never reach the agent process, so these must be
declared as environment variables, not exported in the script:

| Variable | Value | Why |
| --- | --- | --- |
| `UNITARES_SERVER_URL` | `https://<allowlisted-host>` | Loopback default is meaningless in a container; must be https on an allowlisted host |
| `UNITARES_HTTP_API_TOKEN` | client bearer token | Without it writes are unattributable |
| `UNITARES_FILE_LEASES_ENABLED` | `0` | No lease plane in-container; it already fails open, so this is tidiness |

**3. Add the server's hostname to the environment's network allowlist.** Steps
1 and 2 are wasted without it — this is the step that actually decides whether
governance is ONLINE.

Leaving step 3 undone is a legitimate choice. A cloud session then runs with
governance tools and OFFLINE hooks, which is a coherent posture: reads and
manual `sync_state` calls still work through the MCP connector, and only the
automatic lifecycle is absent.

## What this does not cover

The audit establishes that the hooks run and fail open in a residentless
container. It does not establish that the full lifecycle behaves correctly
against a *reachable* server from a cloud session — no such server was
available to test against. Onboarding, check-in submission, and KG recall over
the egress proxy remain unverified.
