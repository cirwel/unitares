defmodule UnitaresSentinel do
  @moduledoc """
  Wave 1 — Sentinel-on-BEAM. Per `docs/proposals/beam-wave-1-sentinel.md`
  v0.1.1 (council-folded).

  Top-level invariant inherited from the lease plane: **BEAM owns live
  coordination, Python owns governance truth, Postgres owns durable truth.**
  Nothing in this app may silently become source of truth for identity,
  EISV, KG, or calibration. Sentinel reads from Postgres + WebSocket
  feeds and emits findings + EISV check-ins via REST to the Python
  governance MCP.

  ## State surfaces (5 total per v0.1.1)

    1. `STATE_FILE` cycle state at `~/.unitares/anchors/.sentinel_state`
    2. Findings emit channel via `post_finding(...)` → `POST /api/findings`
    3. Lease-advisory scope `resident:/sentinel_cycle`
    4. Python-runtime-specific anyio mitigations (BEAM-side: not inherited)
    5. `SESSION_FILE` at `~/.unitares/anchors/sentinel.json` — governance
       identity continuity (binding: schema MUST stay forwards-compatible
       with Python's `GovernanceAgent._ensure_identity`)

  ## Bootstrap status

  This module + its `Application` supervisor are the minimum scaffold per
  the §Bootstrap spec (B5 reviewer fold). The `/ws/eisv` ingest boundary,
  cycle worker, Surface 2
  forced-release alarm findings client, Surface 3 lease-advisory wrapper,
  Surface 4 runtime timeout, and Surface 5 session anchor reader/backup
  boundary and fleet-analysis finding reducer are wired; full runtime
  `sentinel_finding` emission parity lands in follow-up PRs.
  """
end
