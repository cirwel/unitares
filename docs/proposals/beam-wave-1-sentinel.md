# Wave 1 RFC: Sentinel-on-BEAM

**Status:** v0.1.2, 2026-05-05. Surface 1 council pass shipped corrections; binding amendment blocks below. **Read v0.1.2 AMENDMENT first**, then v0.1.1, then v0.1 body as historical record.
**Parent:** `docs/proposals/beam-footprint-roadmap-v0.md` v0.3 / v0.3.1 (operator-decision migration commit + council fold).
**Sibling:** `docs/proposals/surface-lease-plane-v0.md` (Phase A complete, BEAM service running on `127.0.0.1:8788`).
**Bootstrap shipped:** PR #373 — `elixir/sentinel/` skeleton + `AtomicWrite` helper.
**Council pass v0.1.1 (2026-05-05):** dialectic-knowledge-architect (3B/1C/1N), feature-dev:code-reviewer (3B/2C), live-verifier (3 VERIFIED, 1 DRIFT — line numbers, 0 REFUTED). Six BLOCKs, three CONCERNs, one NIT, one DRIFT — all folded inline below.
**Council pass v0.1.2 (2026-05-05):** Surface 1 design council. Architect (2B/3C/1N), reviewer (2 Critical/2 Important), live-verifier (4 VERIFIED, 3 REFUTED, 1 PARTIAL, 1 SOURCE_ONLY). Three BLOCKs, four CONCERNs, several factual REFUTEDs against v0.1.1's Surface 1 prose — all folded in the v0.1.2 amendment block below.

---

## V0.1.2 AMENDMENT 2026-05-05 — Surface 1 council fold (binding spec)

**Read this first if you're touching Surface 1.** v0.1.1 §Surface 1 prose contained three BLOCK-level errors that the live-verifier caught against the actual `agents/sentinel/agent.py` source. v0.1.2 corrects them. This amendment IS the binding spec for Surface 1; the v0.1.1 §Surface 1 paragraph is superseded on every point of conflict.

### B1 (verifier-grounded) — STATE_FILE path correction

v0.1.1 Surface 1 (line 211) said `~/.unitares/anchors/.sentinel_state`. **This is wrong.**

**Actual path** (verified by live-verifier reading `agents/sentinel/agent.py:61`):

```python
STATE_FILE = project_root / ".sentinel_state"
```

where `project_root = Path(__file__).parent.parent.parent` resolves to `/Users/cirwel/projects/unitares/.sentinel_state`. Anchors directory at `~/.unitares/anchors/` holds `chronicler.json`, `sentinel.json`, `steward.json`, `vigil.json`, `watcher.json` — **not** `.sentinel_state`.

**Fold (binding):**

- BEAM Sentinel MUST resolve `STATE_FILE` from a config-supplied absolute path, NOT `Path.expand("~/.unitares/anchors/.sentinel_state")`. Recommended config key: `:unitares_sentinel, :state_file_path` with default sourced from `UNITARES_SENTINEL_STATE_FILE` env var. Production launchd plist sets the env var to whatever `agents/sentinel/agent.py:61` resolves to.
- The shadow file is at the same directory: same path with `.beam` suffix appended (e.g., `<project_root>/.sentinel_state.beam`).
- Cross-runtime parity: when the Python sentinel's path-resolution logic ever moves (e.g., out of project root), both the launchd env var and the BEAM config key must update together.

### B2 (architect) — Boot logic: max-on-boot, not first-boot-Python

v0.1.1 implied "first boot reads Python's file, all subsequent boots read BEAM's." Architect BLOCK-1: this is a silent correctness bug. If BEAM crashes mid-shadow, on restart it would re-read Python's possibly-stale cursor and regress its own de-dup fence — re-emitting alarms it had already passed.

**Fold (binding):**

- Boot logic: read **both** `STATE_FILE` and `STATE_FILE.beam` if both exist; pick the cursor with the larger `forced_release_alarm.last_event_ts` (ISO-8601 strings compare lexicographically when zero-padded with timezone — Python writes `datetime.isoformat()` with UTC offset, verified by live-verifier).
- If only one file exists, read that one.
- If neither exists, return `%{}` (matches Python's `load_state` fallback at `agents/sentinel/agent.py:501`).
- Empty cursor (`{}`) is treated as "older than any ISO-8601 timestamp" by the max-rule.

### B3 (architect) — Cutover-merge protocol pinned

v0.1.1 said "Cutover flips canonical reader from Python's file to BEAM's file" without defining merge semantics. Three protocols compatible with that text; architect BLOCK-2 forced a choice.

**Fold (binding):** **Max wins** — composes cleanly with B2's boot rule and removes coordination requirement at cutover.

- At cutover, BEAM Sentinel re-reads both files one final time, picks the max cursor, persists to `STATE_FILE.beam`, and stops reading `STATE_FILE`.
- Python Sentinel is unloaded via `launchctl unload <python plist>` first; the cutover script then signals BEAM Sentinel to enter "canonical" mode (mechanism: a config flag in `STATE_FILE.beam` itself — `{"runtime": "beam_canonical", ...}` — readable by both runtimes for forensic clarity).
- Rollback: re-load Python Sentinel; remove `runtime` flag (or set to `"python_canonical"`); BEAM Sentinel reads the flag on next cycle and stops writing to `STATE_FILE.beam`.

### C1 (architect) — fsync trade-off explicit, not NIT-ranked

v0.1.1 §B1 reviewer ranked fsync absence as NIT (correct for `sentinel.json` — regenerable from PG). For Surface 1's `.sentinel_state`, the same NIT-ranking is wrong: this file is a **de-dup fence with no upstream re-derivation path**. The `lease_plane_events` table carries event timestamps, not "already-alarmed" flags. Power-loss between cycle N's `save_state` and the rename-durability moment means cycle N+1 re-emits N's alarms.

**Fold (binding):**

- Surface 1 acknowledges duplicate alarm fire on power-loss as **accepted** (downstream dashboard de-dup absorbs it; alarm fingerprint includes the underlying event_id so server-side `/api/findings` dedup catches duplicates within the dedup window).
- B2's max-on-boot rule mitigates the more common case (clean restart): the surviving file's cursor is preserved.
- If a future incident shows alarm-replay impact >1 per quarter, revisit by adding `:fsync_after_rename` option to `AtomicWrite.write/2`. For now, accept the NIT-ranking on the Python side carries forward.

### C2 (architect) — Cursor divergence observability during shadow

v0.1.1 had no comparator. Architect CONCERN-3: the only signal of mismatch is double-emit at cutover, which is exactly the symptom shadow is supposed to prevent.

**Fold (binding):**

- Surface 1 PR includes a `mix sentinel.cursor_diff` Mix task that prints both files' cursors + their delta. Operator runs ad-hoc during shadow window.
- Optional follow-up: `sentinel_shadow_drift` finding emitted once per cycle when `|python_cursor − beam_cursor| > 1 cycle interval`. Severity `info`. Defer to second Surface 1 PR if first PR scope is already at the threshold.

### B4 (reviewer) — `HOME` env var must be set in BOTH plists

Reviewer Critical-2 (verifier-grounded): the existing Python Sentinel plist at `scripts/ops/com.unitares.sentinel.plist` does NOT set `HOME` in `EnvironmentVariables`. The lease-plane plist (`com.unitares.lease-plane.plist:34`) DOES. Python Sentinel's `STATE_FILE` resolution doesn't use `Path.home()` so the gap is dormant — but `SESSION_FILE` (line 59) DOES use `Path.home()`, relying on passwd-database fallback. BEAM's `Path.expand("~")` reads `HOME` only and returns the literal `"~"` if unset.

**Fold (binding):**

- Surface 1 PR amends `scripts/ops/com.unitares.sentinel.plist` to add `<key>HOME</key><string>/Users/cirwel</string>` (matching lease-plane convention).
- The future BEAM Sentinel plist MUST set `HOME` explicitly. Include this in the PR that lands the BEAM plist.
- This is preventive — Surface 1's `STATE_FILE` resolution per B1 above doesn't use `Path.expand("~")`, but Surface 5 (SESSION_FILE) does, and the `HOME` gap is a landmine for any code path that drifts toward tilde-expansion.

### C3 (reviewer) — String-keyed map contract pinned

`Jason.decode/1` produces string-keyed maps; `Jason.encode!/1` normalizes atom keys to strings on write. A BEAM caller using atom keys (`%{forced_release_alarm: ...}`) gets a silent round-trip-through-disk surprise: subsequent reads return `%{"forced_release_alarm" => ...}` (string keys), and `state[:forced_release_alarm]` returns `nil`.

**Fold (binding):**

- `UnitaresSentinel.CycleState` enforces string-keyed maps at the boundary. Either: (a) typespec `@type t :: %{String.t() => term()}` + a runtime check that raises on atom keys, or (b) call `state |> Jason.encode!() |> Jason.decode!()` to normalize before persisting. (b) is simpler; (a) is faster.
- ExUnit tests pin: an atom-keyed input round-trips and the caller sees string keys back.

### C4 (architect) — Schema-compat enforced via Tier 2 contract test

v0.1.1 §Test strategy listed Tier 2 as "cross-runtime contract tests" generically. v0.1.2 specifies the Surface 1 contract test concretely.

**Fold (binding):**

- Tier 2 fixture committed: a Python-written `.sentinel_state` blob (use `agents/sentinel/agent.py:save_state` to generate, snapshot byte-for-byte). BEAM `CycleState.load/0` MUST round-trip it without losing `forced_release_alarm.last_event_ts`.
- Symmetric direction: a BEAM-written `.sentinel_state.beam` blob committed as fixture. Python `load_state` MUST read it without raising and MUST recover the cursor.
- Either side losing the cursor is a CI failure.

### Verifier REFUTED — `forced_release_alarm.last_event_ts` cursor read sites

v0.1.1 said "RFC mentions lines 597, 682, 734 for finding emit sites" — implying three cursor-read sites. **Verifier-confirmed:** there is **one** cursor-read site at `agents/sentinel/agent.py:663`:

```python
cursor_str = state.get("forced_release_alarm", {}).get("last_event_ts")
```

Lines 597, 682, 734 are post_finding emit sites for `sentinel_finding`, `sentinel_forced_release_alarm`, and `lease_plane_phase_b_transition` respectively — they emit, they don't read the cursor.

**Fold:** Surface 1 BEAM-side `CycleState` exposes a single read accessor (`get_last_event_ts/0`) and a single write accessor (`update_last_event_ts/1`). The cycle worker (later PR) is the only caller. Mirroring Python's single-read-site discipline.

### Verifier REFUTED — fsync absence in Python's `atomic_write`

v0.1.1 didn't claim Python's helper called fsync, but the council inquiry surfaced it. Verified at `agents/sdk/src/unitares_sdk/utils.py:30-35`:

- Line 30: `tempfile.mkstemp(...)` — creates 0o600 by default
- Line 32: `os.fchmod(fd, mode)` — explicit fchmod to 0o600
- Line 35: `os.replace(...)` — atomic replace (not `os.rename`)
- **No `os.fsync(fd)`** anywhere in the function

BEAM `AtomicWrite.write/2` matches: chmod 0o600, no fsync. C1 above accepts this.

### Verifier REFUTED — line range for `load_state` fall-through

v0.1.1 said "try / except: pass / return {} pattern at lines 499-501." Verifier-corrected:

- Lines 492-501 contain `load_state`. The `try` body is two lines (496-498), with an `isinstance(data, dict)` guard at line 497 that v0.1.1 omitted.
- The `except Exception: pass` is at lines 499-500; `return {}` is at line 501. RFC's "499-501" is structurally correct but understates the body.

**Fold:** BEAM `CycleState.load/0` mirrors the Python isinstance guard: after `Jason.decode`, check `is_map(decoded)` (Elixir's analogue) before returning; on non-map decode results, fall through to `%{}`.

### N1 (reviewer) — `save/1` exception contract: log-and-continue, not raise

Python's `save_state` swallows write failures (`agents/sentinel/agent.py:506-508`); a `save_state` exception does NOT crash the cycle. BEAM's `AtomicWrite.write/2` re-raises on failure. If `CycleState.save/1` propagates the exception, BEAM Sentinel becomes more brittle than Python on ENOSPC / readonly-fs / etc.

**Fold (binding):**

- `CycleState.save/1` wraps `AtomicWrite.write/2` in `try/rescue`, logs at `:logger.warning`, returns `:ok` either way. The cycle worker continues regardless.
- Distinct from `AtomicWrite.write/2` itself, which retains its raise-on-failure contract for callers that DO want to know.

### What v0.1.2 changes vs v0.1.1

- §Surface 1 path corrected (B1)
- §Surface 1 boot rule: max-on-boot (B2)
- §Surface 1 cutover-merge protocol: max wins (B3)
- §Surface 1 fsync trade-off explicit (C1)
- §Surface 1 cursor-divergence observability requirement (C2)
- §Boundary `HOME` env var binding for both plists (B4)
- §Surface 1 string-key contract pinned (C3)
- §Test strategy Tier 2 contract test for Surface 1 spelled out (C4)
- Cursor read site corrected from "597, 682, 734" to "663" (verifier REFUTED)
- Python `atomic_write` fsync absence noted; BEAM matches (verifier REFUTED on fsync claim)
- `load_state` line-range corrected to include isinstance guard (verifier REFUTED)
- `CycleState.save/1` exception contract: log-and-continue (N1)

### What v0.1.2 does NOT change

- v0.1.1 binding spec for Surfaces 2–5 is unchanged.
- Migration commitment (operator decision under v0.3) unchanged.
- Wave 1 = Sentinel-on-BEAM unchanged.
- Sibling Elixir app at `elixir/sentinel/` unchanged (PR #373 already shipped this).
- 4 stop signs unchanged.
- Exit criteria gate on ODE profile unchanged.

---

## V0.1.1 AMENDMENT 2026-05-05 — council fold (binding spec)

**Read this first.** v0.1 was drafted from a static read of the codebase and missed several load-bearing details. v0.1.1 supersedes the v0.1 spec on every point of conflict; v0.1 body is preserved below as historical record. This amendment IS the binding RFC.

### B1 (architect) — Surface 5: `SESSION_FILE` and governance identity continuity

v0.1 listed only four state surfaces. The session anchor at `~/.unitares/anchors/sentinel.json` is itself a load-bearing surface, not Open Question Q1.

**Why it's load-bearing:**

- `GovernanceAgent._ensure_identity` reads `agent_uuid` + `continuity_token` from this file. With `refuse_fresh_onboard=True` (`agents/sentinel/agent.py:477`), BEAM Sentinel will **refuse to start** if the anchor is missing or schema-skewed.
- More critically: during shadow mode (per v0.1 §Surface 1), Python and BEAM Sentinel each call `process_agent_update` on every cycle. **Two parallel Sentinels writing to the same agent's per-agent state is a real per-agent state write**, not a "Sentinel doesn't write per-agent state" no-op as v0.1 framed it. The dashboard `/ws/eisv` stream picks up both runtimes' EISV updates and FleetState (which Sentinel itself ingests via WS, see C3 below) loops on its own observations.

**Fold (binding):**

- **No shadow mode for the agent_uuid.** Cutover is direct flip on identity: BEAM Sentinel re-uses the same agent_uuid + continuity_token by reading the existing `sentinel.json`. Python Sentinel's launchctl service is unloaded at the cutover moment; BEAM Sentinel's launchctl service is loaded immediately after.
- **State format compatibility (binding):** BEAM Sentinel MUST NOT modify `sentinel.json` schema beyond what Python `GovernanceAgent` expects. Adding a `runtime: "beam"` field to metadata is OK (forwards-compat); modifying `agent_uuid` or `continuity_token` shape is forbidden.
- **Backup before cutover:** `cp ~/.unitares/anchors/sentinel.json ~/.unitares/anchors/sentinel.json.pre-beam` is a binding step in the deploy procedure. Rollback restores from this backup.

### B2 (architect) — Findings emit endpoint + fingerprint format

v0.1 §Boundary said BEAM Sentinel calls `http://127.0.0.1:8767/v1/tools/call` with `tool=leave_note`. **This is wrong.**

**Actual contract:** `agents/common/findings.py:18-19` posts to `http://localhost:8767/api/findings` with the JSON body shape defined in that file. The dedup fingerprint format at `agents/common/findings.py:24-32` is `compute_fingerprint(["sentinel", finding_type, violation_class, agent_id])` returning a 16-hex prefix. The server uses this fingerprint to suppress duplicates.

**Fold (binding):**

- Endpoint: `POST http://127.0.0.1:8767/api/findings`. Not `/v1/tools/call`.
- Fingerprint format MUST match Python's exactly. The hash inputs (`["sentinel", finding_type, violation_class, agent_id]`) and the hex prefix length (16 chars) are binding.
- **Tier 2 cross-runtime contract test (binding addition):** given identical (finding_type, violation_class, agent_id) inputs, BEAM and Python MUST produce identical 16-hex fingerprints. Without this test, dedup breaks silently and double-emit happens regardless of cutover semantics.

### C3 (architect) — Asymmetry rationale corrected

v0.1 argued findings can't shadow because "dashboard double-fires." Server-side dedup at `/api/findings` would actually suppress duplicates IF fingerprints match (B2). The real reason direct-flip is correct:

**Sentinel's check_in cycle calls `process_agent_update` (governance EISV write) every cycle. Two parallel Sentinels emit two EISV streams. The WebSocket consumer at `agents/sentinel/agent.py:565` ingests the dashboard's `/ws/eisv` feed back into `FleetState` — Sentinel's own observations of itself become input to its analysis. Two parallel Sentinels create a self-ingestion loop, not just a dashboard double-fire.**

**Fold:** v0.1 §Surface 2 rationale corrected. Direct-flip is binding; shadow mode for findings is structurally unsafe.

### B5 (architect) — Q2 default + §Observability missing

**Q2 resolution (binding):** REST. BEAM Sentinel calls governance MCP via `POST /api/findings` and `process_agent_update` via existing REST surface. NOT via hex.pm Elixir MCP SDK for Wave 1. Reasoning: MCP-direct from BEAM creates a cross-runtime protocol coupling that Wave 3 (which migrates the MCP server itself) would have to either preserve or break — exactly the substrate-tax pattern stop sign #4 is designed to catch. REST preserves the boundary contract that's already proven via lease plane Phase A.

**§Observability (binding new section):**

- BEAM Sentinel writes logs to the same path Python uses: `~/Library/Logs/unitares-sentinel-beam.log` (note `-beam` suffix to keep streams separate during shadow / for forensics post-cutover). Rotation: same `MAX_LOG_LINES=1000` semantics as Python (`agents/sentinel/agent.py:62`).
- Log format MUST be parseable by existing `tail -f data/logs/...` workflows — structured logging via `Logger.metadata` is fine but the human-readable line MUST start with `[YYYY-MM-DDThh:mm:ss]`.
- launchd plist `StandardErrorPath` and `StandardOutPath` redirect to `~/Library/Logs/unitares-sentinel-beam.{out,err}.log` for BEAM stack traces and supervisor output. Application-level findings + cycle progress go to the rotated `unitares-sentinel-beam.log`.

### B1 (reviewer) — `atomic_write` equivalence

v0.1 said `File.write/2` + `File.rename/2` is "equivalent to Python's `atomic_write`." It is not, in three ways:

- Python's `atomic_write` (`agents/sdk/src/unitares_sdk/utils.py:17-48`) uses `tempfile.mkstemp` (creates 0o600) + `os.fchmod(fd, 0o600)` + `os.replace`. `File.write/2` creates with the process umask (typically 022 → 0o644 on launchd) — **mode regression on a security-relevant cursor file**.
- Python's helper has a `finally:` cleanup of the orphan `.tmp` file. Bare Elixir `File.write/2 + File.rename/2` does not.
- fsync is absent in both Python and Elixir paths (NIT-level on macOS APFS, but call it out so a future BLOCK doesn't surprise).

**Fold (binding):**

```elixir
defmodule Sentinel.AtomicWrite do
  def write(path, content) do
    tmp = path <> ".tmp"
    try do
      :ok = File.write!(tmp, content)
      :ok = File.chmod!(tmp, 0o600)
      :ok = File.rename!(tmp, path)
    rescue
      e ->
        File.rm(tmp)
        reraise e, __STACKTRACE__
    end
  end
end
```

This helper is binding for `.sentinel_state` writes. Direct `File.write/2` to the cursor path is forbidden.

### B2 (reviewer) — `Mint.WebSocket` is not in the dep tree

v0.1 named `Mint.WebSocket` without a hex package version pin and without specifying reconnect / ping behavior.

**Fold (binding):**

- **Hex package:** `{:mint_web_socket, "~> 1.0"}` added to `elixir/sentinel/mix.exs` deps.
- **Consumer topology:** the WebSocket consumer is a `GenServer` (not a bare `Task`), owning the reconnect state explicitly. Reconnect on any error with 10s backoff (matching Python's `await asyncio.sleep(10)` at `agents/sentinel/agent.py:537`).
- **Ping behavior:** disable application-level pings to match Python's `ping_interval=None` (`agents/sentinel/agent.py:521`). Loopback connection — TCP detects drops.
- **Message buffering:** if reconnect happens mid-stream, BEAM Sentinel does NOT replay missed events. FleetState is rebuilt incrementally from current state on next message — same posture as Python.

### C3 (reviewer) — Byte-equivalence downgraded to structural-equivalence

v0.1 §Surface 2 promised "byte-equivalent where possible." Achievable on fingerprint inputs (B2 above) but NOT on full JSON body shape because:

- Jason sorts map keys alphabetically by default; Python `json.dumps` preserves dict insertion order.
- ISO-8601 timestamps: Postgrex's `DateTime.to_iso8601/1` produces `Z`-terminated strings; Python's `datetime.isoformat()` on tz-aware values produces `+00:00`-terminated strings.

**Fold (binding):**

- Tier 2 contract test asserts **structural equivalence + named-field contract**, not byte-equivalence. Required fields per finding type enumerated in test fixtures.
- **Fingerprint test stays byte-equivalent** (16-hex-prefix string comparison).
- v0.1 "byte-equivalent" claim retracted.

### C4 (reviewer) — Audit-outbox NOT inherited; PeriodicWorker IS

`elixir/lease_plane/lib/unitares_lease_plane/audit_outbox_forwarder.ex` projects `lease_plane.lease_plane_events` → `audit.tool_usage`. **Sentinel does NOT use this pattern** because Sentinel reads from `lease_plane_events` and emits to `/api/findings` over HTTP, not to a DB outbox.

**Fold (binding):**

- BEAM Sentinel implementation MUST NOT inherit `AuditOutboxForwarder` from lease plane. Cargo-cult risk warning explicit in this RFC.
- BEAM Sentinel SHOULD inherit `PeriodicWorker` from lease plane (`elixir/lease_plane/lib/unitares_lease_plane/periodic_worker.ex`). The 300s analysis cycle maps cleanly onto `PeriodicWorker` with `interval_ms: 300_000`.
- The `start_workers: false` test gate from `elixir/lease_plane/config/test.exs` SHOULD be inherited so ExUnit tests can drive cycles deterministically.

### B5 (reviewer) — §Bootstrap spec (binding new section)

v0.1 assumed `elixir/sentinel/` into existence with no app-skeleton spec.

**Fold (binding):**

- **OTP app name:** `:unitares_sentinel`. Module namespace: `UnitaresSentinel.*`.
- **Path:** `elixir/sentinel/` (sibling to `elixir/lease_plane/`).
- **`mix.exs` deps (minimum):**
  - `{:postgrex, "~> 0.20"}` — Postgrex for `lease_plane_events` polling
  - `{:jason, "~> 1.4"}` — JSON for findings emission
  - `{:mint_web_socket, "~> 1.0"}` — WebSocket consumer (per B2 reviewer fold)
  - `{:finch, "~> 0.18"}` — HTTP client for `/api/findings` POSTs (Mint-based, hex.pm production-grade)
  - `{:stream_data, "~> 0.6", only: :test}` — property tests for fingerprint equivalence
- **DB env var:** `UNITARES_SENTINEL_DATABASE_URL` (separate from `UNITARES_LEASE_PLANE_DATABASE_URL` so deployment can pin a read-only role for Sentinel). Falls back to `LEASE_PLANE_*` if unset (compat default).
- **Bearer token env vars:** `LEASE_PLANE_BEARER_TOKEN` for lease plane API; `UNITARES_HTTP_API_TOKEN` for `/api/findings` (governance MCP).
- **CI integration (binding):** `mix test` for `elixir/sentinel/` runs in the same CI gate as the Python suite. New CI step in `.github/workflows/` (or equivalent) to be added by the Wave 1 implementation PR. Tier 1 ExUnit tests + lease plane tests + Python suite all gate the merge.
- **Test harness:** `test/test_helper.exs` boots Postgrex sandbox + a fixture for `lease_plane_events` rows. Reuses `elixir/lease_plane/test/support/` patterns where applicable.

### N4 (architect) — Sibling app correct, stated for the record

`elixir/lease_plane/mix.exs` is a flat single-app project (`Mix.Project`, not umbrella). Adding `elixir/sentinel/` as sibling matches existing topology and isolates Sentinel's deps. Umbrella promotion (single `elixir/mix.exs` over both apps) deferred to Wave 3+ when more apps land. **No change needed; stated here so the next reviewer doesn't re-litigate.**

### Verifier DRIFT — Line citations off by +1

v0.1 cited lines drafted against an earlier file state. Master HEAD `cf144993` line numbers (corrections):

- `load_state` / `save_state`: `agents/sentinel/agent.py:492-510` (was 492-509)
- `sentinel_finding`: `:597` (was 596)
- `sentinel_forced_release_alarm`: `:682` (was 681)
- `lease_plane_phase_b_transition`: `:734` (was 733 — verifier confirmed 734)
- `lease_advisory_scope`: `:549-554` (verified, range fits)
- `_poll_sync_forced_release` `asyncio.run()`: `:449-453` (verified exact)
- `refuse_fresh_onboard=True`: `:477` (was 476)

All patterns + counts confirmed by verifier. Citations updated; substance unchanged.

### What V0.1.1 changes vs V0.1

- §State migration: 5 surfaces (added Surface 5 — SESSION_FILE + identity continuity)
- §Surface 1: atomic_write helper specified (§B1 reviewer)
- §Surface 2: endpoint corrected to `/api/findings`; fingerprint contract binding
- §Surface 2 rationale: corrected to WS/EISV self-ingestion loop, not dashboard double-fire
- §BEAM↔Python boundary: REST resolved (Q2), endpoint correct, WebSocket consumer spec'd
- §Test strategy: byte-equivalence downgraded to structural-equivalence + fingerprint byte-equivalence
- §Observability: NEW SECTION (B5 architect)
- §Bootstrap spec: NEW SECTION (B5 reviewer)
- §AuditOutboxForwarder: explicit NOT-inherit warning + PeriodicWorker DO-inherit
- Line citations corrected (verifier DRIFT)
- Q1 promoted to Surface 5 (was open question)
- Q2 resolved (was open question)

### What V0.1.1 does NOT change

- Migration commitment unchanged (operator decision under v0.3 stands).
- Wave 1 = Sentinel-on-BEAM unchanged (lowest blast radius for agent-state DB layer).
- Sibling Elixir app at `elixir/sentinel/` unchanged.
- 4 stop signs unchanged.
- Exit criteria gate on ODE profile result unchanged (v0.3.1 C1 fold preserved).

---

## Why Wave 1 is Sentinel

v0.3 §Sequencing names Sentinel-on-BEAM as the smallest first ship under A′ (stateful-coordinating to BEAM, stateless-computing stays Python). v0.3.1 council fold corrected the over-confident "read-mostly" framing; this RFC is the work artifact that addresses the four state surfaces v0.3.1 enumerated. **Sentinel still has the lowest blast radius of any Wave candidate** because it does not write to the agent-state DB, does not hold the per-agent governance lock, and does not gate any user-visible request path. It does own four other state surfaces this RFC has to migrate cleanly.

## Scope

**In scope:**

- Port `agents/sentinel/` analysis-cycle loop to a sibling Elixir OTP app at `elixir/sentinel/`.
- Migrate the four Sentinel-owned state surfaces (per v0.3.1 B1) with explicit cutover semantics.
- Replace Python-runtime-specific anyio mitigations (`asyncio.run()` inside thread executor at `agents/sentinel/agent.py:449-453`) with BEAM-native async patterns.
- Maintain exact behavioral parity in findings emission: the BEAM Sentinel must produce the same `post_finding` shapes (`sentinel_finding`, `sentinel_forced_release_alarm`, `lease_plane_phase_b_transition`) as the Python Sentinel, byte-equivalent where possible.
- Use the lease plane Phase A advisory pattern (no Phase B enforcement needed for Sentinel — it does not write agent state).
- Define a launchctl plist for the BEAM Sentinel and a documented rollback to the Python Sentinel.

**Out of scope:**

- Wave 3 work (handler dispatch, identity middleware, dialectic resolution). Lock-invariant inventory belongs in Wave 3 RFC, not here.
- ODE profile work (`process_update_authenticated_async` profiling). Runs in parallel; lands in v0.3.1.1 amendment; gates Wave 1 *exit criteria authorship* (per v0.3.1 C1) but not implementation.
- Vigil and Chronicler ports. Each gets its own RFC if/when sequenced.
- Phase B `resident:/` enforcement window (v0.3.1 C3). Sentinel uses Phase A advisory; opening Phase B for resident surfaces is a Wave 3 prerequisite, not a Wave 1 prerequisite.
- Migration of the `unitares_sdk` Python SDK to an Elixir SDK. Cross-runtime, BEAM Sentinel calls governance MCP via the same HTTP/REST contract Python Sentinel uses today.

## State migration (per v0.3.1 B1, B3, B4)

Sentinel owns four state surfaces. Each surface gets explicit cutover semantics.

### Surface 1: `STATE_FILE` cycle state at `~/.unitares/anchors/.sentinel_state`

**Current owner:** `agents/sentinel/agent.py:492-509` (`load_state()` / `save_state()`).
**Critical contents:** `forced_release_alarm.last_event_ts` cursor (the de-duplication fence for alarm replay).
**Format:** JSON, written atomically via `unitares_sdk.utils.atomic_write`.

**Cutover semantics:**

- **Default: shadow mode for ≥1 cycle of meaningful traffic before flip.** BEAM Sentinel reads the existing `.sentinel_state` on first boot; Python Sentinel keeps writing during the shadow window; BEAM Sentinel writes a parallel file at `.sentinel_state.beam` that BEAM uses for its own cursor advancement during shadow. Cutover flips canonical reader from Python's file to BEAM's file.
- **Format compatibility (binding):** BEAM Sentinel MUST use the same JSON schema as Python's `load_state()` reader expects. No nested-object additions to existing keys without a corresponding migration shim. The `forced_release_alarm.last_event_ts` MUST stay an ISO-8601 string at the top level of the cursor object.
- **Rollback compatibility:** if Wave 1 is rolled back mid-cycle, Python `load_state()` (`agents/sentinel/agent.py:492`) MUST be able to read whatever `.sentinel_state` BEAM last wrote without zeroing the cursor. The `try / except: pass / return {}` pattern at lines 499-501 is the failure mode that loses the cursor; rollback procedure relies on the file staying schema-compatible.

**Implementation note:** BEAM-side persistence uses `File.write/2` to a temp path + `File.rename/2` for atomic write semantics. Equivalent to `atomic_write` Python helper.

### Surface 2: Findings emit channel via `post_finding(...)`

**Current owner:** `agents/sentinel/agent.py:596` (`sentinel_finding`), `:681` (`sentinel_forced_release_alarm`), `:733` (`lease_plane_phase_b_transition`).
**Downstream:** dashboard subscribers (WebSocket from broadcaster), Discord bridge, KG (per `docs/proposals/sentinel-events-vs-kg.md`).

**Cutover semantics:**

- **No shadow mode for findings emit.** Two parallel Sentinels emitting findings would double-fire dashboard alerts. Cutover is direct flip: Python Sentinel stops emitting on launchctl unload; BEAM Sentinel starts emitting on launchctl load. Gap window MUST be <30s (one cycle interval at low edge).
- **Behavioral parity bar:** BEAM Sentinel's emitted finding payloads MUST match Python Sentinel's exactly for the three event types. Test fixture: a known forced-release event in PG produces byte-equivalent `sentinel_forced_release_alarm` finding from both runtimes (modulo timestamp + agent_uuid, which are runtime-bound).
- **Rollback fits inside the same direct flip.** Stop BEAM Sentinel; start Python Sentinel; no findings persist mid-flip.

### Surface 3: Lease-advisory scope `resident:/sentinel_cycle`

**Current owner:** `agents/sentinel/agent.py:549-554` via `from src.lease_plane.advisory import lease_advisory_scope, new_holder_uuid`.
**Mode:** Phase A advisory — failed acquire MUST NOT block normal operation (per surface-lease-plane-v0.md §6.1).

**Cutover semantics:**

- **No state migration.** Lease plane is the same BEAM service for both runtimes. BEAM Sentinel calls the lease plane HTTP API (`POST /v1/lease/acquire`) using the documented bearer-auth pattern. The TTL (300s), surface_id, and intent string stay identical.
- **Holder UUID change OK.** Each Sentinel runtime mints its own holder_agent_uuid per cycle (already does — `new_holder_uuid()` is per-call). Cutover doesn't carry holder identity.
- **Rollback OK.** Both runtimes use the same lease plane API; either can acquire the surface advisory.

### Surface 4: Python-runtime-specific anyio mitigations

**Current owner:** `agents/sentinel/agent.py:449-453` (`_poll_sync_forced_release` uses `asyncio.run()` inside a thread executor specifically to escape the anyio loop).

**Cutover semantics:**

- **Pattern does not exist in BEAM.** BEAM has no anyio loop; the workaround is structurally unnecessary. Replacement: BEAM Sentinel polls Postgrex directly from a `Task.async/1` with `Task.await/2` at the same 30s timeout. No equivalent of the thread-executor escape hatch is needed because Postgrex is async-native to the BEAM scheduler.
- **No state to migrate.** This is a runtime mechanism, not a state surface; calling it out here only because v0.3.1 B1 enumerated it.

## Rollback procedure (per v0.3.1 B4)

If Wave 1 hits Stop sign #1 (Sentinel-on-BEAM produces measurable contention or coordination failure that doesn't exist on Sentinel-as-Python today), rollback steps:

1. `launchctl unload ~/Library/LaunchAgents/com.unitares.sentinel-beam.plist` — stop BEAM Sentinel.
2. Verify `.sentinel_state` (canonical, not the `.beam` shadow) still exists and parses as JSON. If the file is corrupt, restore from `.sentinel_state.bak` (BEAM Sentinel MUST keep a 1-cycle-old backup at this path during operation).
3. `launchctl load ~/Library/LaunchAgents/com.unitares.sentinel.plist` — start Python Sentinel. Existing plist preserved through Wave 1 — NOT removed even after BEAM Sentinel ships, until a v0.3.2 explicitly retires it.
4. Verify Python Sentinel's first cycle after rollback emits a `sentinel_finding` event (sanity check) and does NOT re-fire all forced-release alarms. If alarm replay storm starts, the cursor was zeroed — investigate immediately.
5. The BEAM Sentinel's `sentinel.json` session anchor at `~/.unitares/anchors/sentinel.json` (or wherever GovernanceAgent persists it) MUST NOT be modified by BEAM in any way that breaks Python's `refuse_fresh_onboard=True` guard at `agents/sentinel/agent.py:476`. If BEAM uses a different session anchor file, this is fine. If BEAM re-uses the same anchor, the format is binding-compatible.

**Stop sign threshold for triggering rollback:** more than 3 cycles in 1 hour where BEAM Sentinel's lease acquire fails when the same surface_id is held by the operator's manual claim, OR more than 1 forced-release alarm replay (cursor regression).

## Test strategy (per v0.3.1 C4)

**Layered approach:**

### Tier 1: ExUnit unit tests (BEAM-side)

- Cycle-loop driver test: drives a fixture EISV event stream into the BEAM Sentinel's WebSocket consumer, asserts FleetState mutations match expected snapshot.
- Forced-release polling test: drives fixture `lease_plane_events` rows into Postgrex sandbox, asserts `last_event_ts` cursor advances correctly + correct alarms emit.
- State persistence test: writes a known cursor, restarts the GenServer, asserts cursor recovery from `.sentinel_state` (or `.sentinel_state.beam` during shadow mode).

### Tier 2: Cross-runtime contract tests (Python suite)

- Existing `tests/test_sentinel_forced_release_alarm.py` stays as the Python-side regression bar. The 8329-test Python suite remains the acceptance gate for Python Sentinel during the transition window.
- New cross-runtime fixture: same input event stream, both runtimes emit findings, byte-equivalence asserted modulo runtime-bound fields (timestamps, agent_uuid).

### Tier 3: End-to-end integration test

- Lease plane round-trip: BEAM Sentinel acquires `resident:/sentinel_cycle`, holds for cycle duration, releases. Python integration test drives this from outside, asserts lease state observable via REST API matches expected lifecycle.
- Findings emit smoke: BEAM Sentinel emits a `sentinel_finding`; dashboard WebSocket subscriber receives it; Discord bridge picks it up. Manual smoke acceptable for Wave 1 ship; automation via Tier 1+2 sufficient for regression.

**Minimum bar before Wave 1 ships:** all Tier 1 + Tier 2 green; Tier 3 lease round-trip automated; Tier 3 findings emit verified manually.

## BEAM↔Python boundary

**Pattern:** lease plane Phase A advisory. Reused as-is. BEAM Sentinel calls `POST http://127.0.0.1:8788/v1/lease/acquire` (and `/release`) with bearer auth from `LEASE_PLANE_BEARER_TOKEN` env var. Same contract Python Sentinel uses today via `src/lease_plane/advisory.py`.

**Findings emit:** BEAM Sentinel calls the governance MCP at `http://127.0.0.1:8767/v1/tools/call` with `tool=leave_note` (or equivalent post_finding tool). HTTP/REST, no SDK surface needed BEAM-side. No special boundary protocol.

**WebSocket consumer:** BEAM Sentinel connects to `ws://127.0.0.1:8767/ws/eisv` directly using a BEAM WebSocket client (`Mint.WebSocket` or equivalent). Same endpoint Python Sentinel uses.

## Exit criteria (gates on ODE profile per v0.3.1 C1)

**Wave 1 ships when:**

1. All four state-surface migrations have shadow-mode evidence ≥1 cycle of meaningful traffic.
2. All Tier 1 + Tier 2 tests green.
3. Tier 3 lease round-trip automated and green.
4. ODE profile result has landed (v0.3.1.1 amendment) — needed to write meaningful exit criteria for "BEAM dissolved the ceiling" claim. If the ODE is numpy compute, Wave 1 still ships, but the exit criterion changes to "Wave 1 produces parity, Wave 3 sequencing weakens" rather than "Wave 1 validates the architectural premise."
5. Council pass on this RFC has folded findings inline (this draft has not had council pass yet — see §"Council pass" below).

## Wave 0 instrumentation requirements

Per v0.3 §Sequencing, Wave 0 is the channel for measuring whether the migration is succeeding. For Wave 1 specifically:

- **Boundary substrate-tax watch (per v0.3.1 stop sign #4):** any new `coordination_failure.*` event_type that appears post-Wave-1 deploy on the BEAM↔Python boundary surfaces MUST be cataloged. If >1 distinct workaround pattern accrues at the boundary, halt before Wave 3.
- **Cycle-cadence delta:** Sentinel cycle interval is 300s in Python. BEAM Sentinel MUST hold the same cadence; any drift >5% over a 24h window is a regression worth investigating.
- **Findings-emit rate parity:** BEAM Sentinel SHOULD emit roughly the same number of `sentinel_finding` events per hour as Python Sentinel did pre-cutover. Significant under- or over-emission is a regression.

## Stop signs

1. **Lease acquire fails** repeatedly on a surface_id that Python Sentinel had no trouble with. Indicates BEAM-side bearer auth wiring or holder-UUID generation broken.
2. **Forced-release alarm replay storm** on rollback or restart. Indicates `.sentinel_state` cursor was zeroed — schema compatibility broken.
3. **Boundary substrate-tax** (per v0.3.1 stop sign #4): >1 distinct workaround pattern at the BEAM↔Python boundary.
4. **Findings-emit drift** measurably degrades dashboard / Discord-bridge observability vs Python Sentinel.

Any of (1)-(4) triggers Wave 1 rollback per §"Rollback procedure" above. (3) is a Wave-1-implementation review gate, not a runtime stop sign.

## Open questions

- **Q1: Does BEAM Sentinel re-use the Python `sentinel.json` session anchor**, or mint a separate one? Re-using means rollback compatibility is automatic but introduces a coupled file format. Separate means cleaner Wave 1 boundary but BEAM Sentinel needs to onboard fresh on first deploy (interacting with `refuse_fresh_onboard=True` — operator action needed).
- **Q2: Does BEAM Sentinel call governance MCP via REST (`/v1/tools/call`) or via the MCP protocol directly** using one of the new hex.pm Elixir MCP SDKs (`mcp_elixir_sdk` 1.0.1 or `hermes_mcp` 0.14.1, per v0.3.1 B5 finding)? REST is simpler and preserves the boundary contract that's already proven; MCP-direct is more ambitious and would close part of the v0.1 SDK gate ahead of schedule.
- **Q3: Do we use ETS, Mnesia, or just file-on-disk for cycle state across BEAM Sentinel restarts?** v0.3.1 B3 default presumption is "BEAM does NOT modify Python-readable file format until Wave-N+1 explicitly changes the canonical reader" — strongest argument for sticking with the JSON file on disk for Wave 1.

## Council pass

**Required before this RFC is binding.** Same pattern as v0.3.1: 3 agents in parallel (architect / reviewer / live-verifier), scoped adversarial-on-technical-detail. Specific framing:

- **Architect lane:** does the four-surface migration actually capture all of Sentinel's state? Are there hidden coupling points between Sentinel and the rest of the system that this RFC misses? Is the shadow-mode-for-cursor / direct-flip-for-findings asymmetry the right call?
- **Reviewer lane:** when this RFC becomes Elixir code, what cross-cutting concerns does it underestimate? Read the existing `elixir/lease_plane/` app for patterns; is the BEAM Sentinel a sibling app or nested inside? Test coverage gaps?
- **Live-verifier lane:** every code path / file path cited in this RFC against master HEAD. Findings emit shape (do `sentinel_finding`, `sentinel_forced_release_alarm`, `lease_plane_phase_b_transition` actually exist in the codebase?). Lease plane API contract still as described?

Council ack-pass before Wave 1 implementation PR opens. Findings folded inline as `## v0.1.1 AMENDMENT — council fold` at the top of this doc.

## Cross-references

- **Parent roadmap:** `docs/proposals/beam-footprint-roadmap-v0.md` (v0.3 + v0.3.1).
- **Boundary pattern source:** `docs/proposals/surface-lease-plane-v0.md` Phase A.
- **Sentinel current implementation:** `agents/sentinel/agent.py`, `agents/sentinel/forced_release_alarm.py`, `agents/sentinel/sitrep.py`, `agents/sentinel/fleet_state.py`.
- **Existing Elixir app pattern:** `elixir/lease_plane/`.
- **Memory anchors:** `project_substrate-question-governance-mcp.md` (v0.3 decision), `project_plexus-coordination-layer.md` (lease plane state), `feedback_substrate-migration-status-quo-bias.md` (pole-flipped under operator authorization).
