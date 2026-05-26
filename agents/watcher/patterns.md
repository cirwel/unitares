# Watcher Pattern Library

Curated bug patterns for the Watcher agent. Seeded from real incidents in the
UNITARES project. The Watcher feeds this file to a local LLM (qwen3-coder-next via Ollama)
and asks for pattern-matches against recently edited code.

**Edit freely.** Add patterns you've been bitten by. Remove patterns that produce
too many false positives. The Watcher reloads this file on every run.

## Severity levels

- **critical** — data loss, security, irrecoverable state
- **high** — runtime failure, resource exhaustion, deadlock
- **medium** — degradation, leaks, unbounded growth
- **low** — smell, style, best-practice

## Patterns

### P001 — Fire-and-forget task leak (severity: high, violation_class: ENT)

Creating `asyncio.create_task(...)` inside a loop or per-event handler without
storing the task reference for later cancellation or cleanup.

**Seen in:** `background_tasks.py` stuck_agent_recovery_task (2026-04-10 incident,
1.1GB RSS runaway)

**SAFE — DO NOT FLAG:**
```python
# Pattern A: task ref stored in a set with done-callback cleanup
task = loop.create_task(some_coro())
_background_tasks.add(task)
task.add_done_callback(_background_tasks.discard)

# Pattern B: task ref stored in a variable, later cancelled and awaited
ws_task = asyncio.create_task(self.ws_consumer())
# ... later in the same function ...
ws_task.cancel()
await ws_task
```

If the task reference is assigned to a variable or added to a collection
(set, list, dict) in the same block, it is NOT fire-and-forget. Only flag
when the return value of `create_task()` is completely discarded or used
only in a bare expression statement.

The structural verifier drops any P001 finding whose flagged line matches
`name = ...create_task(...)` — the assignment is sufficient evidence that
the ref is stored. See `_P001_TASK_ASSIGNMENT` in `agent.py`.

The verifier also drops findings on `create_tracked_task(...)` call sites:
that helper is the project's blessed wrapper which stores the task ref in
a tracked set by construction. False-positive sweep 2026-04-17: flagged
two sites in `mcp_server_std.py:511,551` (both `create_tracked_task(...)`
calls). See `_P001_TRACKED_HELPER` in `agent.py`.

**Hint template:** `fire-and-forget task — store ref or use TaskGroup`

### P002 — Unbounded dict/list growth (severity: medium, violation_class: ENT)

`dict[key] = value` or `list.append(x)` inside a loop or per-event handler
without a cap, LRU eviction, or periodic sweep.

**Seen in:** `adaptive_prediction.py`, `serialization.py` (Ogler finds, 2026-04),
`lifecycle_events` cap fix (2026-04-07)

**Hint template:** `unbounded growth — needs cap or eviction`

### P003 — Transient monitor pattern (severity: high, project-specific, violation_class: ENT)

Creating a `UNITARESMonitor(agent_id)` instance outside of
`mcp_server.get_or_create_monitor()`. The cached factory inserts into
`mcp_server.monitors`; bypassing it creates throwaway instances that never enter
the cache and cause init storms over time.

**Seen in:** `stuck.py:175-186` (2026-04-10 incident)

The structural verifier drops findings whose flagged line lives inside the
body of `def get_or_create_monitor(` itself — that function IS the cache,
not a transient call site. False-positive sweep 2026-04-17: flagged
`agent_lifecycle.py:26` (the `monitor = UNITARESMonitor(agent_id)` line
that the cache uses to populate itself). See
`_is_inside_get_or_create_monitor` in `agent.py`.

**Hint template:** `transient monitor — use get_or_create_monitor`

### P004 — Unguarded Redis call inside MCP tool handler (severity: high, project-specific, violation_class: REC)

Any `await` on a raw Redis async client inside an `@mcp_tool`-decorated handler
(functions in `src/mcp_handlers/`). The anyio task group in the MCP SDK's
StreamableHTTP transport deadlocks with unbounded Redis async calls. Symptom:
`/v1/tools/call` hangs indefinitely for that tool.

**Scope narrowed 2026-05-23: Redis only.** asyncpg ops in MCP handlers are
*safe* post-PR #218 (2026-04-27), which wraps the asyncpg pool in
`src/db/executor_pool.py`. Direct `await conn.fetchval(...)`,
`await db.acquire()`, and `await mcp_server.load_metadata_async()` no longer
collide with the anyio task group — the asyncpg work happens on a dedicated
background thread. Historic dismiss rate on asyncpg-firing P004s was 89.5%
(17 dismissed / 19 fired) post-#218; the two `confirmed`s were correct
*before* #218 and are stale. Per CLAUDE.md "Substrate Tax" section:
*"New handlers can use `async with db.acquire() as conn: await
conn.fetchval(...)` directly — no wrapper needed for asyncpg DB work."*

Redis is **NOT** ExecutorPool-wrapped. Existing Redis `asyncio.wait_for`
timeouts in `identity_step.py`, `persistence.py`, `session.py` remain
load-bearing; do not remove them, and do not add new unguarded `await redis.*`
in MCP handlers.

**SAFE — DO NOT FLAG:**
```python
# Starlette REST route handlers in src/http_api.py (http_health, http_metrics,
# http_incidents, etc.) run in normal asyncio context, NOT inside the MCP
# SDK's anyio task group. asyncpg and Redis are both safe in REST endpoints.
async def http_incidents(request):
    events = await query_audit_events_async(...)  # safe — REST handler

# MCP helper whose public entrypoint bounds the async Redis work.
async def lookup_onboard_pin(fp):
    return await asyncio.wait_for(_lookup_onboard_pin_inner(fp), timeout=0.5)

# asyncpg in MCP handlers — safe via ExecutorPool (post-PR #218):
@mcp_tool("my_tool")
async def handle_my_tool(arguments):
    async with db.acquire() as conn:
        row = await conn.fetchval("SELECT ...")  # safe — ExecutorPool-wrapped
```

**FLAG:**
```python
@mcp_tool("my_tool")
async def handle_my_tool(arguments):
    val = await redis.get(key)  # P004 — unguarded Redis in MCP handler
```

Only flag `await` on raw Redis calls in files under `src/mcp_handlers/` or in
functions decorated with `@mcp_tool` / `@mcp.tool()`. Do NOT flag plain
Starlette route handlers in `src/http_api.py`, and do NOT flag asyncpg ops
(those go through ExecutorPool).

The structural verifier enforces both constraints post-hoc: P004 findings
outside `src/mcp_handlers/` are dropped, and the flagged line must contain a
literal Redis call marker (`await redis`, `redis.get(`, `redis.set(`, etc.).
See `_PATTERN_FILE_PATH_CONSTRAINTS` and `_PATTERN_REQUIRED_TOKENS["P004"]`
in `agent.py`.

**Seen in:** `health_check` MCP tool (fixed via Option F), KG lifecycle, eisv_sync.
False-positive sweep 2026-04-14 (flagged `async def http_dashboard` and
unrelated arithmetic in `src/http_api.py`) motivated the verifier constraints.
Scope-narrowing sweep 2026-05-23 dismissed 9 stale asyncpg findings and
removed asyncpg tokens from `_PATTERN_REQUIRED_TOKENS["P004"]`.

**Hint template:** `unguarded Redis in MCP handler — wrap with asyncio.wait_for(..., timeout=N)`

### P005 — Acquire without paired release (severity: high, violation_class: REC)

`pool.acquire()`, `lock.acquire()`, `connection.cursor()`, or similar resource
acquisitions without a paired release in a `finally:` or `async with` context.

**Hint template:** `acquired resource not released on all paths`

### P006 — Silent exception swallow (severity: medium, violation_class: VOI)

`except Exception: pass` or `except Exception: logger.warning(...)` without
re-raising. Hides real bugs and makes debugging impossible.

**SAFE — DO NOT FLAG:**
```python
# Non-critical side-effect wrapped in its own try/except
# (broadcasting, telemetry, optional notifications)
try:
    await broadcaster.broadcast_event("some_event", ...)
except Exception as e:
    logger.debug(f"Could not broadcast: {e}")
```

If the swallowed code is an optional side-effect (broadcast, telemetry,
metrics emission) inside its own isolated try/except block — and the
primary logic path does NOT depend on its result — it is intentional.
Only flag when the swallow is on the main logic path or could mask a
failure the caller needs to know about.

**Hint template:** `silent swallow — log and re-raise or narrow the except`

<!-- P007 has been demoted to the EXPERIMENTAL section below.
     Detecting it requires reasoning about temporal flow (which pool was
     acquired vs. which is being released to), which the local 8B model
     can't do reliably without flagging the FIX as a bug. See the
     experimental section for the original definition. -->


### P008 — Unchecked shell input (severity: critical, violation_class: VOI)

Fire **only** when both conditions hold:
1. The call is `subprocess.*(..., shell=True)` OR `os.system(...)` OR `os.popen(...)`.
2. The command string includes user/external input without `shlex.quote`.

**Do NOT fire** on list-form subprocess calls — those bypass the shell entirely
and are the recommended safe form. Examples that must NOT be flagged:
```python
subprocess.run(["find", path, "-name", "*.py"], check=True)
subprocess.run(["wc", "-l"] + files, capture_output=True)
subprocess.Popen(["git", "log", "--oneline"])
```

Examples that SHOULD be flagged:
```python
subprocess.run(f"find {path} -name '*.py'", shell=True)  # unquoted interpolation
os.system("rm -rf " + user_input)                         # shell + external input
```

**Hint template:** `shell injection — use shlex.quote or list-form subprocess`

### P009 — Runaway polling without iteration cap (severity: medium, violation_class: ENT)

`while True:` or `while condition:` loops that poll for state with `sleep`
without a max-iteration guard or timeout. Can hang agents indefinitely if the
expected state change never arrives.

**Hint template:** `unbounded poll — needs max-iteration or timeout`

### P010 — Missing test coverage on behavior change (severity: medium, violation_class: INT)

New behavior (a bound, cap, eviction, cleanup branch) added without a matching
test. This is a standing rule for this project — see
`feedback_tests-with-fixes.md`.

**Hint template:** `behavior change needs test in same commit`

### P011 — mutate-then-persist in memory (severity: high, project-specific, violation_class: INT)

Mutating in-memory state BEFORE (or WITHOUT) the corresponding DB persistence
call. The temporal ordering matters: **persist must come first**, then mutate.

**BAD (flag this):**
```python
meta.status = "archived"           # in-memory mutation
await archive_agent(agent_id)      # persist comes after — race & clobber risk
```

**ALSO BAD (flag this):**
```python
meta.status = "archived"           # mutation with no persist call anywhere
# (nothing else)
```

**GOOD — DO NOT FLAG:**
```python
await archive_agent(agent_id)      # persist first
meta.status = "archived"           # mutation comes after — correct ordering
```

If you see `await <something_persist_like>(...)` BEFORE the mutation in the
same function/block, the code is correctly ordered. Do not flag it.

**Seen in:** `auto_archive_orphan_agents` in `agent_lifecycle.py:134-148` (the
pre-fix version was archiving 73 agents on every cron cycle with no persistence
at all). The fix added `await archive_agent()` before the in-memory mutation —
the post-fix code is the GOOD example above.

**Hint template:** `mutation before persistence — will be clobbered on next load`

### P012 — json.loads / yaml.load on untrusted input (severity: medium, violation_class: INT)

Parsing JSON or YAML from external sources (HTTP bodies, files, MCP tool args)
without schema validation. Pydantic v2 schemas in `src/mcp_handlers/schemas/`
are the project-standard way.

**Hint template:** `unvalidated parse — add pydantic schema`

### P013 — --no-verify / --amend after hook failure (severity: critical, process, violation_class: VOI)

Not a code pattern but a process one. Never bypass pre-commit hooks with
`--no-verify`, and never `git commit --amend` after a pre-commit hook failure
(the failure means the commit did NOT happen; amend would modify the PREVIOUS
commit and risk losing work). Fix the underlying issue and create a NEW commit.

**Hint template:** `bypass/amend after hook fail — fix root cause, new commit`

### P014 — Force push / reset --hard on shared branches (severity: critical, process, violation_class: VOI)

`git push --force`, `git reset --hard origin/X`, `git branch -D` without
explicit user approval. See the 2026-02-25 incident: another Claude session
force-pushed master and lost ~80 commits on the remote.

**Hint template:** `destructive git op — requires explicit user approval`

### P015 — Docker commands against retired containers (severity: medium, project-specific, violation_class: VOI)

Any `docker exec postgres-age` or `docker-compose` command targeting the retired
`postgres-age` container. The canonical database is Homebrew PostgreSQL@17 on
port 5432. Docker postgres-age is retired; commands targeting it will either
fail or hit stale data.

**Hint template:** `docker postgres-age retired — use homebrew psql on 5432`

### P016 — Nested-success-false swallowed in envelope parsing (severity: high, violation_class: INT)

Parsing a wrapped response that has BOTH an outer envelope success flag and a
nested inner success flag, but only checking ONE layer. The outer envelope can
report transport-level success while the inner result reports semantic failure.
Any code that does `if response["success"]: ...` (or `.success`) without also
checking the nested `result.success` is this pattern — and the consequences are
particularly nasty when the code then writes state based on the assumed success.

**BAD (flag this):**
```python
if data.get("success"):
    # writes session file based on nothing — inner may have failed
    write_session(data.get("result", {}))
```

**GOOD — DO NOT FLAG:**
```python
if data.get("success") and data.get("result", {}).get("success"):
    write_session(data["result"])
```

**Seen in:** `scripts/unitares` parse_onboard (fix commit 718ccd3, 2026-04-11).
The server returned `success:true` at the envelope layer and
`result.success:false` with reason `trajectory_required` when the identity
already existed with an established EISV trajectory and needed verification or
`force_new=true`. The CLI's parse_onboard only checked the envelope; cmd_onboard
then wrote an empty `{agent_id: ...}` session file, **clobbering any valid
continuity token that was already there**. Two regression tests in the same
commit now guard against re-introduction
(`test_parse_onboard_detects_nested_success_false`,
`test_onboard_with_force_creates_fresh_identity`).

**SAFE — DO NOT FLAG:**
```python
# Typed pydantic model returned by the SDK — by construction flat
audit_result = await client.audit_knowledge(scope="open")
if audit_result.success:           # attribute access on typed model — flat
    summary["audit_run"] = True
```

The SDK's `call_tool` flattens any envelope (`_parse_mcp_result` +
`_raise_for_tool_failure`) before `model_validate`. Attribute access on a
typed result has no nested success layer to miss. The structural verifier
now requires a quoted `"success"` literal on the flagged line — see
`_PATTERN_REQUIRED_TOKENS["P016"]` in `agent.py`. This drops typed-attribute
false positives while keeping `data["success"]` / `data.get("success")`.

False-positive sweep 2026-04-14: flagged four SDK-typed call sites in
`agents/vigil/agent.py:292,308,318,324` (`result.success`,
`audit_result.success`, `cleanup_result.success`, `orphan_result.success`).
All four are typed pydantic models from `agents.sdk` — no nested envelope
exists. Required-token constraint added to prevent recurrence.

False-positive sweep 2026-05-04: seven findings on the SDK envelope-parsing
code in `agents/sdk/src/unitares_sdk/{client.py,sync_client.py}` (fingerprints
`f5d5a59c`, `85890889`, `3a77f756`, `1492d4d7`, `f1ce4e8d`, `ceed748b`,
`46b7705f`). The SDK's `_rest_call` (sync_client.py:330,340) already does the
correct two-layer check (outer `data["success"]` + `result.get("isError")`)
and unwraps before returning; `_raise_for_tool_failure` is then called on the
already-unwrapped inner result, so its single `raw.get("success") is False`
check is semantically the inner-layer check. Four FP shapes flagged in one
batch:

1. **Path-traversal chained `.get()`** — `raw.get("a", {}).get("b", {}).get("value")`
   for option-extraction; no `success` token on the cited line.
2. **Function signatures and import statements** — flagged as the
   "representative" line for a finding whose actual `success` reference
   lives elsewhere in the same function.
3. **Unrelated-line attribution** — kwargs (`method="POST"`), bare `try:`
   blocks; cited line carries no semantic content.
4. **Already-unwrapped-result check** — single-layer `success` check on
   inner result that the caller has already validated outer-envelope for.

Six of these seven *should* have been dropped by the required-token filter
at `agent.py:1431-1438` since their cited lines do not contain `"success"`.
That they were not dropped suggests the cited line drifted between
detection time and filter time (file edits shifted line numbers; the filter
re-reads `src_line` from current disk state, not the snapshot at detection).
Persisting `src_line` alongside the fingerprint — and replaying the
required-token check against the stored snippet at chime time — would close
this gap and harden the entire detector pipeline, not just P016. Two
sweeps in three weeks is enough evidence that the rule's own escape hatch
(move to experimental) is on the table if the storage fix doesn't ship.

False-positive sweep 2026-05-20: third sweep, two findings (fingerprints
`651b3427` at `sync_client.py:342`, `2b12fa39` at `sync_client.py:458`).
Different shape from the 2026-05-04 batch — both flagged lines DO contain
the quoted `"success"` literal, so the required-token filter could not have
caught them. Instead they are the **already-unwrapped-result check** shape:
the operator wired the inner-layer assertion (`result.get("isError")` at
sync_client.py:352, or the `_raise_for_tool_failure` helper itself), but the
model sees a single `.get("success")` and flags. Closed by adding two
structural verifiers parallel to `_P016_GETATTR_SUCCESS`:

- `_is_p016_followed_by_inner_layer_check` walks forward ~20 lines for
  `isError`, `_raise_for_tool_failure(`, `_parse_mcp_result(`, or
  `<Result>.model_validate(` as the inner-leg cue.
- `_is_p016_inside_inner_assertion_helper` walks backward up to 6 lines for
  a `def _raise_for_*(` enclosing header — by convention these helpers
  operate on already-unwrapped inner results.

The `src_line` storage fix from the 2026-05-04 note remains separately
useful for line-drift FPs and is tracked outside this rule. Move-to-
experimental stays off the table as long as the structural-verifier path
keeps closing new shapes.

**Why this is in the active library and not experimental:** the shape is
reasonably lexical — the model can spot a conditional on one success flag
that ignores a nested success flag in the same response object. If Qwen3
starts false-positiving on legitimate single-layer dict responses (flat
APIs that genuinely use `data.get("success")` once), move this to
experimental.

**Hint template:** `nested result.success not checked — outer envelope lies`

### P017 — Bare await in daemon/launchd script without timeout (severity: high, violation_class: REC)

Any `await` on a network call (MCP, HTTP, WebSocket) inside a script intended
to run under launchd or as a `--once` daemon, without wrapping in
`asyncio.wait_for(coro, timeout=...)`. If the remote never responds, launchd's
`StartInterval` will skip subsequent invocations while the prior instance is
still alive — the daemon silently stops running.

**Seen in:** `heartbeat_agent.py --once` parked on an MCP call for days under
launchd (2026-04 incident). Fixed with `asyncio.wait_for(CYCLE_TIMEOUT)`.

**SAFE — DO NOT FLAG:**
```python
await asyncio.wait_for(self._bounded_analysis_cycle(), timeout=CYCLE_TIMEOUT)
```

Only flag bare `await some_network_call()` without a surrounding
`wait_for` or `async_timeout` in files that are entry points for
launchd plists or `--once` CLI modes.

**Hint template:** `bare await in daemon — needs asyncio.wait_for timeout`

## Experimental patterns

These are real bug shapes that the 8B local model cannot reliably detect
without false-positiving on the FIX for the bug. They're documented here so
the knowledge isn't lost. Re-promote them once we have either (a) a structural
verifier in `watcher_agent.py` or (b) a larger model with stronger temporal
reasoning.

### EXP-P007 — Path acquired from one pool, released to another (high, violation_class: REC)

Using `postgres_backend.py` pool helpers where `acquired_pool` is not tracked
and the connection gets released to a different pool than it was acquired from.

**Why disabled:** Detecting this requires distinguishing the bad shape (no
`acquired_pool` field) from the fix shape (`acquired_pool` is tracked and
release is gated on `current_pool is acquired_pool`). The local model flags
both as P007. Needs an AST-based verifier that walks the class and confirms
no per-pool tracking exists. See `src/db/postgres_backend.py:170-205` for the
post-fix reference shape.

**Seen in:** `src/db/postgres_backend.py` pool mismatch bug

## Adding new patterns

When Watcher flags a real bug you would have missed, add a new pattern here
with:
1. A unique `Pxxx` id
2. A severity
3. A "Seen in:" reference with the commit or incident date
4. A hint template Watcher should use when surfacing the pattern

The Watcher rewards you for curation: confirmed finds on a pattern raise its
priority; dismissed finds lower it. Over time the library becomes a bespoke
bug-hunter tuned to your codebase.
