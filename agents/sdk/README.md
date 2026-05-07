# unitares-sdk

Build your own Unitares resident agent. A resident is a long-running (or
scheduled) process that checks in to governance, carries an EISV state
vector, and participates in the shared knowledge graph. Vigil, Sentinel,
and Chronicler are reference implementations.

## The 30-line resident

```python
from pathlib import Path
from unitares_sdk.agent import CycleResult, GovernanceAgent
from unitares_sdk.client import GovernanceClient


class MyResident(GovernanceAgent):
    def __init__(self):
        super().__init__(
            name="MyResident",
            mcp_url="http://127.0.0.1:8767/mcp/",
            persistent=True,               # protects from auto-archive
            refuse_fresh_onboard=True,     # explicit bootstrap required
            cycle_timeout_seconds=60.0,    # hard cap on one cycle
            log_file=Path("/tmp/my_resident.log"),
            max_log_lines=10_000,
        )

    async def run_cycle(self, client: GovernanceClient) -> CycleResult | None:
        # Do your work here. Return a CycleResult to trigger a check-in,
        # or None to skip (useful for "nothing to do this tick" paths).
        count = await self.do_scan(client)
        if count == 0:
            return None
        return CycleResult(
            summary=f"scanned {count} items",
            complexity=0.2,
            confidence=0.9,
        )


if __name__ == "__main__":
    import asyncio
    asyncio.run(MyResident().run_forever(interval=60))
```

First run: `UNITARES_FIRST_RUN=1 python my_resident.py` — this mints
the identity and stores its UUID anchor at
`~/.unitares/anchors/myresident.json`. Every subsequent run resumes
that anchor automatically. Never delete anchors: if you do, set
`UNITARES_FIRST_RUN=1` again to re-bootstrap (you will get a new UUID).

## Extension points

The base class handles MCP connect, identity resolve, check-in,
heartbeat, log rotation, state persistence, and graceful shutdown.
Override these to extend behavior:

| Hook | When | Signature | Return |
|---|---|---|---|
| `run_cycle(client)` | Each iteration. The only required override. | `(client) -> CycleResult \| None` | `CycleResult` or `None` |
| `on_after_checkin(client, checkin_result, cycle_result)` | After each check-in, even on pause/reject. Use for EISV logging, coherence tracking, state writes that need the server response. | All three args typed via `unitares_sdk.models`. | `None` |
| `on_verdict_pause(client, checkin_result, cycle_result)` | When the server returns a `pause` verdict. Use for self-recovery. | Same arg signature as `on_after_checkin` for consistency. | `True` to retry the check-in once; `False` to let `VerdictError` propagate. |

Hook exceptions are logged and swallowed — a broken hook cannot take
down a cycle. `asyncio.CancelledError` always propagates.

Do **not** override `_ensure_identity`, `_handle_cycle_result`, or
`_send_heartbeat` — those are load-bearing and change across versions.

## Constructor reference

| Parameter | Type | Default | Purpose |
|---|---|---|---|
| `name` | `str` | required | Agent display name; drives anchor path (`~/.unitares/anchors/<name_lower>.json`). |
| `mcp_url` | `str` | `http://127.0.0.1:8767/mcp/` | Governance MCP endpoint. |
| `persistent` | `bool` | `False` | Stamp the `persistent` + `autonomous` tags on fresh onboard. Set `True` for long-running residents. |
| `refuse_fresh_onboard` | `bool` | `False` | Require `UNITARES_FIRST_RUN=1` to mint a new identity. Set `True` to prevent silent ghost-forks. |
| `cycle_timeout_seconds` | `float \| None` | `None` | Hard cap on a single `run_once` / `run_forever` iteration. MCP's anyio task group can hang on `session.initialize` if the server flakes — use 60–120s. |
| `log_file` | `Path \| None` | `None` | Log file to auto-trim after each cycle (in both `run_once` and `run_forever`, in a `finally` block so it fires on error and timeout too). Leave unset if launchd / logrotate owns rotation. |
| `max_log_lines` | `int` | `10_000` | Trim threshold for `log_file`. |
| `state_dir` | `Path \| None` | `<repo>/data/<name_lower>` | Default directory for state persistence. Used when `state_file` is not set. |
| `state_file` | `Path \| None` | `None` | Explicit cross-cycle state path. Takes precedence over `state_dir / "state.json"` when set. |
| `parent_agent_id` | `str \| None` | `None` | Forked-from UUID. Forwards to server on fresh onboard. |
| `spawn_reason` | `str \| None` | `None` | One of `compaction`, `subagent`, `new_session`, `explicit`. |

## Lifecycle shapes

- **Daemon:** `asyncio.run(agent.run_forever(interval=60))` — loops
  forever with heartbeats when idle. Reference:
  `agents/sentinel/agent.py`.
- **Scheduled:** `asyncio.run(agent.run_once())` under launchd /
  systemd cron. References: `agents/chronicler/agent.py`,
  `agents/vigil/agent.py`.

Both shapes respect `cycle_timeout_seconds` and auto-trim `log_file`.

## State persistence

- `self.save_state(d: dict)` and `self.load_state() -> dict` let your
  `run_cycle` carry data across iterations. Writes are atomic
  (`os.replace`).
- Non-JSON-serializable values (e.g. `datetime`, `Path`) are coerced
  to their `str()` representation on save rather than raising
  `TypeError`. You get a lossy round-trip, not a silent state-write
  failure.

## Identity rules

1. The agent's first MCP call (`onboard` or `identity`) is the sole
   source of identity. Do not set identity out-of-band.
2. UUID is the ground truth. `client_session_id` and
   `continuity_token` are cache keys for ephemeral clients; residents
   don't need them.
3. Anchors live at `~/.unitares/anchors/<name_lower>.json`. One
   anchor per host per role. The file contains `agent_uuid` and is
   written atomically.
4. Never silent-swap an identity. If the anchor is missing and
   `refuse_fresh_onboard=True`, `_ensure_identity` raises
   `IdentityBootstrapRefused` — the operator must explicitly set
   `UNITARES_FIRST_RUN=1` once to mint a new one.

## Not in the SDK (on purpose)

- `agents/common/findings.py`, `agents/common/taxonomy.py`, and
  `agents/common/config.py` are internal to the reference residents
  in this repo. If you need findings-posting in your own resident,
  vendor the helper or POST to `/api/findings` yourself — the REST
  contract is the public surface.
- Watcher (`agents/watcher/agent.py`) uses a different execution
  model (sync, hook-driven, one-shot per tool-use event) and does not
  subclass `GovernanceAgent`.
