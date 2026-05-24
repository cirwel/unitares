# agents/

Reference implementations of Unitares governance agents. These are **not** part of the public contract — the public contract is [`unitares-sdk`](sdk/) (published as its own package). Treat the code under `vigil/`, `sentinel/`, `chronicler/`, and `watcher/` as examples of how to build a resident agent, not as load-bearing governance internals.

## Layout

| Path        | Role                                                                           |
|-------------|--------------------------------------------------------------------------------|
| `sdk/`      | **Integral.** Public agent-to-governance contract (`unitares-sdk` package).   |
| `common/`   | Shared helpers used by the residents in this tree (findings, config, log, taxonomy). |
| `vigil/`    | Reference **janitorial** resident — runs on a schedule, posts health findings. |
| `sentinel/` | Reference **fleet-monitor** resident — continuous, WebSocket-driven.          |
| `chronicler/` | Reference **archive** resident — daily external-source capture.            |
| `watcher/`  | Reference **code-watcher** resident — wired into Claude Code's PostToolUse hook. |

## Running your own

To deploy your own residents, depend on `unitares-sdk` and follow the `run_cycle` pattern shown in `vigil/agent.py` or `sentinel/agent.py`. Lumen (the embodied agent) lives in a separate repo (`anima-mcp`) and is another example of an SDK consumer.

## LaunchAgents (Mac)

- `com.unitares.vigil` — runs `vigil/agent.py --once` every 30 min
- `com.unitares.sentinel-beam` — active BEAM Sentinel cutover slot
- `com.unitares.sentinel` — Python Sentinel reference / rollback slot
- `com.unitares.chronicler` — runs `chronicler/agent.py` daily

Plist templates: `scripts/ops/`.
