# The agent orchestrator's build lane (`build.claude`)

A build lane runs Claude Code unattended in one working directory with a
brief the operator fixed, through a named launch profile of the agent
orchestrator (`docs/proposals/active/inference-hosts-config-v0.md` §2.5). It
exists so that an agent with little quota can commission work from one with
more: the requester prepares a directory and asks the orchestrator for a
build; the builder works inside that directory; the requester publishes what
came out. The orchestrator ships the lane; what a build does, where the
directories come from and how results are published belong to the project
that uses it.

## What the profile fixes

| Setting | Source | Meaning |
|---|---|---|
| `BUILD_CLI` | the claude CLI the generator found (`UNITARES_CLAUDE_CLI` override) | the CLI run, by its symlink path |
| `BUILD_ROOTS` | `--build-root`, repeatable (`AGENT_ORCHESTRATOR_PROFILES_BUILD_ROOTS`, `:`-separated) | directories a build directory may sit under; never `/`, the home directory or a parent of it |
| `BUILD_BRIEF` | `--build-brief` (`AGENT_ORCHESTRATOR_PROFILES_BUILD_BRIEF`) | the brief, read when the build starts and sent to the CLI on standard input; refused if group- or world-writable |
| `BUILD_READ_ROOTS` | `--build-read-root`, repeatable (`..._BUILD_READ_ROOTS`) | directories the file tools may also read (`--add-dir`) |
| `BUILD_MCP_CONFIG` | `--build-mcp-config` (`..._BUILD_MCP_CONFIG`) | the only MCP servers the CLI loads (`--strict-mcp-config`); none without it |
| `max_runtime_ms` | fixed, 7 200 000 | the orchestrator kills the whole process tree at two hours; the client stops the CLI at 7 000 s so its result file is written first |
| `max_concurrent` / `max_per_hour` | fixed, 1 / 4 | one build at a time |
| `lineage` | `caller` | the request may carry `{parent_agent_uuid, spawn_reason}`; the runner provisions them to the child as `UNITARES_PARENT_AGENT_ID` / `UNITARES_SPAWN_REASON`, and the governance plugin's session-start hook turns them into the builder's own onboard call |

The lane is omitted without a root and a brief. A root, brief, read root or
MCP file that is named but missing is an error from the generator, not a
silent omission: the doctor's `orchestrator_profiles` check replays the
recorded inputs, and a lane that silently disappeared would read as current.

A request fills `BUILD_DIR` (an absolute path, resolved with `realpath`,
accepted only at or under a root, never a `.env` or `.unitares` path) and,
optionally, `BUILD_MODEL`. Nothing else: the request's own text never reaches
the model except as files the caller put in the directory.

## What the client does

`src/mcp_handlers/support/build_lane_client.py`, run with `python -I`:

1. Validates the directory, brief, roots and model, and writes
   `BUILD_DIR/.build-lane/started.json`.
2. Builds the child environment from `claude_cli_client.ENV_ALLOWLIST` (the
   subscription login through `HOME` and `USER`; no console-API credentials,
   provider switches or the orchestrator's bearer) plus the provisioned
   lineage variables, with `GIT_CONFIG_GLOBAL=/dev/null` and
   `GIT_CONFIG_NOSYSTEM=1` so no credential helper, alias or hook path from
   the operator's configuration is in reach.
3. Puts a private shim directory first on `PATH`: `gh` and `unitares-github`
   exit 3 ("the caller publishes"); `git` refuses `push`, `fetch`, `pull`,
   `remote`, `config`, `credential`, `submodule` and `add -f`, then hands
   everything else to the real `git`.
4. Runs `claude -p` with the brief on standard input, `--strict-mcp-config`
   (`--mcp-config FILE` when fixed), `--tools Read,Edit,Write,Glob,Grep,Bash,
   WebFetch,WebSearch,Agent` and the same names in `--allowedTools` (nothing
   prompts), a `--disallowedTools` list (`git push`, `gh`, `launchctl`,
   `open`, `osascript`, `ssh`, `sudo`, `.env` and `.unitares` reads),
   `--permission-mode dontAsk --permission-prompts none`,
   `--no-session-persistence`, `--max-turns 400`, `--output-format
   stream-json --verbose`, `--add-dir` per read root and `--model` when
   given. The working directory is `BUILD_DIR`, in its own process group.
   It does **not** pass `--safe-mode` or `--restricted`: the project's
   instructions, the operator's hooks and the governance plugin are what make
   an unattended build behave like an attended one, and Bash is what runs a
   project's pipeline.
5. Streams the CLI's output to its own standard output (the orchestrator
   captures the tail) and to `.build-lane/build.log`; on exit, deadline or
   signal writes `.build-lane/result.json` with the exit status, times, the
   CLI's final result text, model, turn count and cost where reported, and
   `killed` (`null`, `deadline` or `signal`).

Exit statuses: the CLI's own; 2 for the client's refusals, 124 at the
client's deadline, 127 for a missing CLI.

## What this does not bound

Bash is not path-confined, and a process running as the operator's user can
read whatever that user can, the GitHub App's key included. The shims and
tool rules are guardrails against a builder going off-script, not a security
boundary against a hostile one. The trust in the request content comes from
the caller:

- build from a base the caller trusts, and copy in only what it validated
  (an allowlisted subtree of the request, never the request's whole tree,
  which could carry changed instructions, hooks or pipeline code);
- pin every git operation to the exact commit validated, not a mutable ref;
- publish only after checking what the build changed against the paths it
  was expected to change, and never force-add ignored paths;
- never send the request's text to the orchestrator or into the brief.

The orchestrator's bearer is held by the caller, never by the requester, and
never reaches the child.

## Operating it

Settings for the orchestrator's `elixir/agent_orchestrator/scripts/start.sh`, beside the existing profile settings:

```sh
AGENT_ORCHESTRATOR_PROFILES_FILE=...            # already required for any profile
AGENT_ORCHESTRATOR_PROFILES_GENERATE=1
AGENT_ORCHESTRATOR_PROFILES_BUILD_ROOTS=/path/to/worktrees
AGENT_ORCHESTRATOR_PROFILES_BUILD_BRIEF=/path/to/BRIEF.md
AGENT_ORCHESTRATOR_PROFILES_BUILD_READ_ROOTS=/path/one:/path/two   # optional
AGENT_ORCHESTRATOR_PROFILES_BUILD_MCP_CONFIG=/path/to/mcp.json      # optional
```

Setting `AGENT_ORCHESTRATOR_PROFILES_FILE` turns raw spawns off by default;
an installation whose other callers still spawn raw must set
`AGENT_ORCHESTRATOR_RAW_SPAWN=allow` explicitly (the doctor warns while it
stands). The orchestrator regenerates the file every minute from the recorded
inputs and refuses profile spawns while it is stale, so a brief that moves
or a root that disappears stops builds rather than running them against
nothing.

Starting a build:

```sh
curl -s -X POST http://127.0.0.1:8789/v1/agents \
  -H "Authorization: Bearer $AGENT_ORCHESTRATOR_BEARER_TOKEN" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: build:<request>:<commit>" \
  -d '{"profile": "build.claude", "vars": {"BUILD_DIR": "/path/to/worktrees/job"},
       "lineage": {"parent_agent_uuid": "<caller uuid>", "spawn_reason": "build_request-<id>"}}'
```

`POST /v1/executions/:id/await` clamps its timeout to 120 s and a finished
result is retained for five minutes, so a two-hour build is followed by
`GET /v1/executions` (live ids) and the directory's `.build-lane/result.json`,
not by awaiting. Stopping one: `DELETE /v1/executions/:id`, which kills the
process tree (`kill_tree`: children first, then the client; the result file
then records `killed`).

The first consumer is the operator's private task-building project, whose
poller, brief and publishing live in that repository.
