---
status: RETIRED 2026-05-01 (operator decision after v0.4; S6 ambiguous → ambiguous-default-treat-as-falsified honored; no v0.5; surviving work re-scoped to Python discipline track — see §10 RETIREMENT entry)
authored: 2026-04-30
amended: 2026-05-01 (v0.4 — S6 spike-result fold-in, post-merge of v0.3 PR #275; first S6 verdict withdrawn after council, corrected re-measurement landed at 11:32:43Z)
amended_prior: 2026-05-01 (v0.3 — S1 fold-in); 2026-04-30 (v0, v0.1, v0.2 same session)
council_pass_1: 2026-04-30
ack_pass_1: 2026-04-30
ack_pass_v0_3: 2026-05-01
ack_pass_v0_4: 2026-05-01
author_session: agent-c9e03e26-33c (claude_code-claude_c9e03e26); v0.3 amendments by agent-05e52624-2a1 (claude_code-claude_05e52624)
review_target: |
  Council pass 1 complete (parallel agents, 2026-04-30; same precedent as
  surface-lease-plane-v0.md):
    - dialectic-knowledge-architect: 3 BLOCKs, 7 CONCERNs, 4 NITs — addressed in v0.1
    - feature-dev:code-reviewer: 4 BLOCKs, 7 CONCERNs, 3 NITs — addressed in v0.1
    - live-verifier: 1 BLOCK, 2 DRIFTs, 5 CONCERNs, 15 VERIFIED — addressed in v0.1

  Ack-pass on v0.1 amendments complete (parallel agents, 2026-04-30; scoped to
  v0.1 amendments only, mirroring surface-lease-plane v0.2.1 ack-pass precedent):
    - dialectic-knowledge-architect: 4 new BLOCKs, 8 new CONCERNs, 3 NITs, 0 DRIFTs — addressed in v0.2
    - feature-dev:code-reviewer: 3 new BLOCKs, 11 new CONCERNs, 1 NIT, 1 DRIFT — addressed in v0.2
    - live-verifier: 0 new BLOCKs, 1 DRIFT, 2 new CONCERNs, 12 VERIFIED, SHIP-with-caveats — addressed in v0.2

  Per the v2.1 precedent, no further ack-pass required after v0.2 unless v0.3+ amendments
  themselves introduce new gaps. Current state: implementation-gate ready.
  The 7 new ack-pass BLOCKs concentrated in the v0.1 surface-area additions
  (bridge process, two-file SHM, supervision-tree split, JSON Schema deliverable);
  all addressed via text-tightening, no architectural revisit required.

  Ack-pass on v0.3 amendments complete (parallel agents, 2026-05-01; scoped to
  v0.3 changes only — S1 result fold-in, §8.2/§8.5 reframing, §7.1 reopen, §9.3.B
  decision-tree restructure):
    - dialectic-knowledge-architect: 2 BLOCKs, 2 CONCERNs, 3 DRIFTs, 1 NIT — NO-SHIP first pass; addressed in v0.3 body before merge (§8.5 conjunction recompute corrected from "three legs / memory leg collapsed" to honest "two-leg unchanged in form, implicit §8.2 prior gone"; §8.1 caveated; §9.3.A pacing split added; §7.1 label tightened; §9.3.B S2-S5 fail-paths mapped)
    - feature-dev:code-reviewer: 1 CONCERN, 2 NITs, SHIP — addressed (S2-S5 fail-paths added to decision tree; §8.2 :erlang.memory() sentence tightened; numeric internal consistency verified)
    - live-verifier: 6 VERIFIED, 2 minor DRIFTs, SHIP-with-caveats — addressed (Elixir runtime banner vs `dpkg -l` package versions separated; tarball byte counts for both default-config and strip_beams rebuild documented)

  Ack-pass on v0.4 amendments complete (parallel agents, 2026-05-01; scoped to
  v0.4 changes only — S6 spike-result fold-in, §8.5/§9.3.B re-framing, §10 v0.4
  entry, methodology lessons; first S6 verdict was withdrawn after a separate
  3-agent council found the verdict's evidence base was thin):
    - dialectic-knowledge-architect: 1 BLOCK, 2 CONCERNs, 1 NIT, 1 DRIFT — NO-SHIP-with-correctable-fix first pass; addressed in v0.4 body before commit (§9.3.B S2 row leg-menu rule re-violation rewritten; default-posture divergence from v0.3 explicitly acknowledged; severity rationale and Pi-CI runner caveat moved out of §10 narrative; "instrument noise" → "axis-weighting variance"; v0.5 framed as one of several outcomes including retire-without-amendment)
    - feature-dev:code-reviewer: 1 BLOCK, 2 CONCERNs, 1 NIT, NO-SHIP — addressed (`45.35 MiB` shiv pyz size unit-convention error in §9.3.B and §10 corrected to `43.346 MiB` from byte count `45,451,738`; downstream `222 MiB total` corrected to `~220.3 MiB`; S2 condition wording rewritten to drop the "fails-cleanly" register-shift; frontmatter status trimmed to one-liner)
    - live-verifier: 8 VERIFIED, 2 minor DRIFTs, SHIP — shiv pyz runnability VERIFIED end-to-end via timeout-and-shutdown trace (Creature 'Lumen' is alive → Entering main loop → Shutdown signal received → exit=124); KG entries verified; PR #275 merge confirmed at 2026-05-01T10:55:34Z; 2 DRIFTs are staleness-of-snapshot (broker uptime advanced 3d→12d since v0.3 measurement; VmRSS drifted +2 MiB over 12 days), not fabrications.

  All v0.4 BLOCKs were addressable via text-tightening; no architectural revisit required.
  All v0.3 BLOCKs were addressable via text-tightening; no architectural revisit required.
provenance: |
  Same-session synthesis. v0 was a single-author sketch (claude_code-claude_c9e03e26,
  2026-04-30) written after operator-decision archaeology (KG
  `2026-04-30T19:30:54.644112+00:00`). v0.1 amendments fold in council pass 1
  findings from three parallel agents in the same session; the council's
  contribution is visible inline (cited section numbers + BLOCK/CONCERN tags).
  This RFC is downstream of the same operator decision that authorized
  surface-lease-plane-v0.md (the Mac-side substrate); both are wedges of the
  same "full BEAM nervous system" destination.
related:
  - docs/proposals/surface-lease-plane-v0.md (Mac-side first wedge; council-clean v0.4; this RFC inherits invariant text but re-states the Pi corollary in §2 to survive Pi-specific contact with broker code)
  - docs/ontology/beam-coordination-kernel.md (parallel ontology-track framing — UNITARES R7 row in `docs/ontology/plan.md`)
  - PR #45 anima-mcp `fix(sensors): server reads SHM, never opens /dev/i2c-1` (the BMP280 wedge — single-writer-to-hardware violation; live-verified)
  - PR #14 anima-mcp `Periodically refresh D22/D24 to prevent TFT blackout` (D22 = TFT backlight + joystick LEFT; D24 = TFT reset + joystick RIGHT; live-verified at `src/anima_mcp/input/brainhat_input.py:65-83` and `display/renderer.py:219-242,327`)
  - PR #11 anima-mcp `fix(sensors): recreate I2C bus handle when multiple sensors fail` (bus-wedge recovery; live-verified)
  - PR #8 anima-mcp `fix(server): swallow MCP SDK ClosedResourceError on client disconnect` (anyio cousin; live-verified)
  - commit `c83748c test: add regression coverage for shutdown ownership + warmup race` (live-verified in git log)
  - `~/.claude/projects/-Users-cirwel/memory/feedback_trust-operator-pattern-over-data-anchor.md` + `anima-mcp/systemd/anima.service:26-31` (BMP280 wedge incident anchor — replaces the earlier wildcard `KG 2026-04-28T*` citation that did not surface in KG search)
  - KG `2026-04-30T19:30:54.644112+00:00` (operator decision: BEAM spike greenlit, full BEAM nervous system as destination — live-verified, exact text match)
out_of_scope_explicit: |
  Hard line — load-bearing substrate boundaries (inherited from `surface-lease-plane-v0.md`):
  - Distributed Erlang clustering between Pi BEAM node and Mac BEAM node (each node single-node; cross-host coordination uses HTTP and Postgres heartbeat-TTL, never Erlang clustering)
  - Identity issuance, EISV math, KG writes, calibration — these stay in Python on the Mac
  - **EISV mapping on the Pi** — see §2 corollary clarification; `anima_to_eisv` and `UnitaresBridge.check_in()` stay in Python as a Pi-resident `unitares-bridge` sidecar process

  Deferred to subsequent RFCs (each merits its own scope):
  - LED hardware ownership cleanup — see §3.4 explicit honesty section; trigger named for v0.5 fold-in
  - Voice/TTS write-path deduplication — server today independently runs `AutonomousVoice` (live-verified at `accessors.py:376-397`); this dual-ownership predates the BEAM port and is out of scope
  - Mic/speaker hardware surface ownership cleanup — same dual-ownership story
  - Phoenix LiveView replacement of TFT display rendering pipeline (Pillow-based today)
  - Cross-language type generation (Pydantic↔Ecto schemas) — v0 ships JSON Schema as the contract floor (§7.6); generated bindings deferred
unblocks: |
  - Single-writer-to-hardware enforcement structurally (today it's convention, repeatedly violated)
  - Shared-pin GPIO races (D22/D24 today coordinated by periodic-refresh hack)
  - Bus-wedge recovery (today: recreate-handle-on-failure heuristic; with OTP: supervisor restarts the GenServer that owns the bus, lease released on death)
  - Distribution: Lumen as appliance ships a single Elixir release tarball, not Python+venv+system-deps+service-files
---

# Proposal: Anima Broker BEAM Port v0 (Pi-side coordination kernel)

> **Status: RETIRED 2026-05-01.** Operator decision after v0.4: S6 landed in the ambiguous band (50-75% on the 70% gate); v0.3's "ambiguous defaults to falsified" rule was kept; no v0.5 issues; surviving work re-scoped to a separate **Python discipline track** (typed-absence server fallback, audit.tool_usage, single-writer hardware ownership checks via lsof CI, watchdog/restart/runbook packaging improvements). Nuitka deferred to a later Python-hardening packaging spike, NOT a blocker for this retirement. No new load-bearing leg invoked. Sections below preserve the design content of the port-that-wasn't for archival reference. See §10 RETIREMENT entry for the operator directive in full.

> **(historical, pre-retirement) Status: DRAFT-v0.4, post-S6 (ambiguous), council ack-pass on v0.4 complete.** S6 ran on Lumen 2026-05-01 after v0.3 council ack-pass cleared. Result: aggregate distribution-win 50-75% at cost-ratio 7.5-25% — qualitatively ambiguous (axis-weighting variance) on the §9.3.B 70%/20% gate. First S6 verdict was WITHDRAWN after a 3-agent council found four classes of fatal evidence/framing errors (broken shiv artifact, hello-world Elixir comparison, killed Nuitka treated as failure, pre-supplied operator branch menu). Corrected re-measurement at KG `2026-05-01T11:32:43.014879+00:00` superseded the withdrawn `11:02:42`. Per §9.3.B v0.3 ambiguous-default-treat-as-falsified posture, v0.4 holds S2-S5 in PAUSED state pending operator decision (one operational step softer than literal retire-default; see §8.5 v0.4 closing acknowledgment). v0.4 council ack-pass found 1 BLOCK (S2 row leg-menu re-violation) + 1 BLOCK (45.35 MiB unit-convention error re-introducing the withdrawn-S6's B3) + several CONCERNs/NITs/DRIFTs — all addressed via text-tightening before commit, no architectural revisit. Follow-on to `surface-lease-plane-v0.md`. The lease plane is the **Mac-side** first wedge for BEAM/OTP; this RFC is the **Pi-side** second wedge: porting the `anima-creature` broker to a single-node Elixir application that owns Lumen's hardware lifecycle. Both nodes are single-node by design (no Distributed Erlang between them); they coordinate via HTTP and Postgres heartbeat-TTL, the same patterns the Python fleet uses today.

## 1. Problem

The Pi-side broker (`anima-creature`, `src/anima_mcp/stable_creature.py`, 1299 LOC Python — live-verified) sits in a class of bugs that OTP was built to make boring. The git trail shows a steady diet:

1. **Single-writer-to-hardware violations.** PR #45 (`fix(sensors): server reads SHM, never opens /dev/i2c-1`) closed the BMP280 wedge: the Python anima-mcp server had been opening I2C directly while the broker also held it. Detection took days because nothing structurally prevented it; lsof eventually showed the violation. Anchor: `feedback_trust-operator-pattern-over-data-anchor.md` (memory) + `systemd/anima.service:26-31` (incident note inline in service file). Replaces the wildcard `KG 2026-04-28T*` citation in v0 that did not surface in KG search.

2. **Bus-wedge recovery as application logic.** PR #11 (`fix(sensors): recreate I2C bus handle when multiple sensors fail`) hand-rolled a recovery routine. This is a manual reinvention of `Supervisor.restart_strategy: :rest_for_one`. **(Genuine OTP-shaped win — see §1.1 bucketing.)**

3. **Shared-pin GPIO races.** PR #14 (`Periodically refresh D22/D24 to prevent TFT blackout`) — TFT backlight (D22) is shared with joystick LEFT; TFT reset (D24) is shared with joystick RIGHT. Periodic 30-second OUTPUT-HIGH re-assertion counters pull-up droop in the absence of a single owner. **(Genuine OTP-shaped win — single GPIO owner via GenServer.)**

4. **Shutdown / warmup ordering races.** Regression test in `c83748c` exists because shutdown ownership and warmup race against each other; tested but not structurally prevented.

5. **anyio-asyncio cousin bugs.** PR #8 (`swallow MCP SDK ClosedResourceError on client disconnect`) is a smaller version of the deadlock class that motivated `surface-lease-plane-v0.md`.

6. **Distribution friction.** Lumen-as-appliance is in the operator goal set. Today's deploy is "Pi reflash → restore_lumen.sh → venv + system deps + service files." This is shippable to operators (Kenny), not to non-developer end-users.

### 1.1 Honest bucketing (revised v0.1)

v0 framed the bucketing as "~5 of 6 in OTP-shaped buckets." Council pass 1 (dialectic) flagged this as over-attribution: any single-writer-discipline fix delivers items 1, 4, 5, 6 — only items 2 (bus recreate via supervisor) and 3 (single GPIO owner) are uniquely OTP-shaped wins. Re-stated:

- **Items 2, 3** — uniquely OTP-shaped (rest_for_one cascade, GenServer-owned pin claim).
- **Items 1, 4, 5, 6** — addressed by *any* substrate change with single-writer-to-hardware discipline; OTP is one solution but not the only one.

The genuine OTP-specific win is **supervision-tree-as-recovery-story** (a structured upgrade and crash-recovery model), not raw fault-class coverage. The §8.1 / §8.5 steelmans below address this directly.

## 2. Decision

Port `anima-creature` to a **single-node Elixir/OTP application** running on the Pi. Hardware ownership lives in OTP processes under a supervision tree; the supervisor IS the recovery story; lease release on death is automatic.

Inherit the lease-plane RFC's invariant verbatim:

> BEAM owns live coordination.
> Python owns governance truth.
> Postgres owns durable truth.
> No BEAM component may silently become source of truth for identity, EISV, KG, or calibration.

### 2.1 Pi corollary (revised v0.1 — addresses dialectic BLOCK §2)

The v0 corollary "BEAM owns hardware lifecycle, Python anima-mcp server stays in Python" was incorrect about current state. **Today the broker calls `UnitaresBridge.check_in()` (`stable_creature.py:925-998` — live-verified) and computes EISV via `anima_to_eisv()` (`:567`).** The broker is the primary UNITARES caller from the Pi.

The corrected corollary, with explicit process placement (revised v0.2 — addresses dialectic ack-pass BLOCKs on SQLite, identity, UNITARES-call channel):

> **BEAM owns hardware lifecycle.**
> **Python owns governance + EISV mapping**, on the Pi via a Pi-resident `unitares-bridge` sidecar process; on the Mac via the governance MCP server. Neither is moved into BEAM.
> **Postgres owns durable truth** on the Mac. **SQLite owns durable truth on the Pi** (`~/.anima/anima.db`); the BEAM broker does NOT hold the SQLite handle. SQLite is opened in **WAL mode with `busy_timeout=5000`** by every reader/writer process; **`unitares-bridge` is the sole writer for governance/EISV state**, **`anima` server is the sole writer for self-model / preferences / learning state**, both processes may read freely. WAL mode + busy_timeout is the explicit concurrency contract; it is not "same as today" hand-wave (today's broker was the only writer; v0 splits writes between bridge and server, which requires the explicit WAL discipline).
> **Identity is bridge-owned**: the bridge writes identity into its SHM channel (`anima_state_governance.json`); the broker does NOT read identity from disk and does NOT pass identity through its own SHM. This eliminates the broker-stale-snapshot failure mode (broker startup-snapshot vs bridge mid-run rebind).
> **UNITARES is bridge-only**: the bridge calls `UnitaresBridge.check_in()`. The broker does NOT call UNITARES directly — it emits operational telemetry only (`audit.tool_usage` rows tagged `source=anima-broker` with no governance payload), via a separate channel from the bridge's governance check-ins. Two callers of UNITARES from the Pi (broker and bridge undifferentiated) is the BMP280 wedge in different uniform; v0 forecloses it.

The Pi-side process layout after v0:

```
  ┌──────────────────────┐    ┌──────────────────────────┐    ┌──────────────────────┐
  │ anima-broker (BEAM)  │    │ unitares-bridge (py)     │    │ anima (py, MCP/HTTP) │
  │  hardware lifecycle  │    │  sensor→EISV→check_in()  │    │  MCP API             │
  │  sensors, GPIO, TFT  │SHM │  reads anima_state.json  │SHM │  reads both SHM      │
  │  face, telemetry     │───▶│  writes anima_state_     │───▶│  files; serves to    │
  │                      │    │    governance.json       │    │  callers             │
  │  audit.tool_usage    │    │  posts to UNITARES       │    │                      │
  │  (operational only)  │    │  governance check-in     │    │                      │
  └──────────────────────┘    └──────────────────────────┘    └──────────────────────┘
            │                            │                           │
            ▼                            ▼                           ▼
       /dev/i2c-1               UNITARES (Mac:8767)             /dev/spidev0.0
       /dev/spidev0.0           (sole UNITARES caller            (LED display)
       GPIO claims               from the Pi)
       (audio out --> see       SQLite (governance/EISV writer)  SQLite (self-model writer)
        AutonomousVoice                                          AutonomousVoice
        on broker; see §3.2)                                     (server-side instance,
                                                                  see §3.2 dual-ownership)
```

The `unitares-bridge` Python sidecar is a new service in v0; its responsibility is `anima_to_eisv` mapping + governance check-in. Code is lifted from `stable_creature.py:567,925-998` essentially unchanged (the actual `bridge.check_in()` call is at `:982-989`; the `:925-998` range contains the call plus surrounding scheduling logic). This avoids the three failure modes the v0 corollary was silent about (porting EISV math into BEAM, growing an undocumented Pi process, or moving per-tick computation across Tailscale).

This invariant is non-negotiable. Any future RFC that proposes moving identity issuance, EISV math, KG writes, or calibration into the BEAM node must reopen the threat model and re-justify the polyglot tax.

## 3. Scope (in / out)

### 3.1 In scope (v0)

- A new Elixir application running on the Pi, separate process from the Python anima-mcp server.
- A new Python `unitares-bridge` sidecar process on the Pi (lifts EISV-mapping + UNITARES check-in code from current broker; not part of v0 Elixir port but is part of v0 deployment). New systemd unit `unitares-bridge.service` — see §6.3 sketch.
- OTP supervision tree owning: I2C bus, BMP280 sensor, accelerometer/gyro sensor, TFT display + reset/backlight pins, joystick GPIO, face-state derivation, metacognitive reflection serialization. **Reflection serialization is under top-level supervisor, not HardwareSupervisor** (council nit — it's not hardware).
- SHM-compatible JSON write to `/dev/shm/anima_state.json` matching the current envelope shape, with the lock-parity contract spelled out in §4.2.
- Health endpoint over HTTP, bound to `127.0.0.1` (council nit — explicit local-only).
- OTP application-controlled clean shutdown ordering. The `os._exit` workaround cited in v0 was already removed in `ed1b2f6` (live-verified) — re-framing: BEAM upgrade story is supervisor restart, replacing Python's PartOf= + explicit-systemctl dance.
- **Operational telemetry only** to UNITARES `audit.tool_usage` via HTTP (no governance, no EISV — those go through the bridge's `UnitaresBridge.check_in()`). Broker emits `tool_name=anima_broker_tick`, `error_type` populated only on hardware errors. **NEW INSTRUMENTATION**: live-verifier confirmed the anima server currently never writes to `audit.tool_usage` (0 rows). The broker's audit channel is therefore a new write path, not an extension; gated by §9.3 spike requirement.
- Vanilla Elixir on Raspbian (council closed §7.1).

### 3.2 Out of scope (v0)

Listed in the frontmatter `out_of_scope_explicit` field. Two items deserve in-body explanation because the v0 wording was misleading:

- **LED hardware ownership** — stays where it is today (server owns LEDs, broker does not import `LEDDisplay` per `stable_creature.py:60`, live-verified). v0 carve-out documented honestly in §3.4.
- **Voice/TTS write-path** — current state is **dual-ownership**, not single-broker-ownership: both broker and server independently instantiate `AutonomousVoice` (live-verified at `accessors.py:376-397`). v0 keeps the broker's voice subsystem in Python (does not port to BEAM, does not change ownership). The dual-ownership cleanup is its own RFC.
- **Mic/speaker hardware surfaces** — `audio/mic.py`, `audio/speaker.py` are real hardware surfaces with the same dual-ownership story. Out of v0 scope.

### 3.3 Promotion bundles (revised v0.1 — addresses code-reviewer BLOCK §6.2)

The v0 8-row surface table assumed surfaces could be promoted independently. Council pass 1 (code-reviewer) flagged this as architecturally incoherent: the broker is a **single synchronous loop** where every surface is computed sequentially from shared sensor input. Reflection cannot be Python-owned while sensors are Elixir-owned — Python broker would have to read-from-SHM-to-write-back, introducing a new race.

Replacing per-row promotion with three **atomic bundles** that mirror the actual loop dependencies:

| Bundle | Surfaces | Phase B promotion order | Notes |
|---|---|---|---|
| **A. Sensors+Anima** | `/dev/i2c-1` bus, BMP280, IMU, GPIO bus, joystick GPIO, raw sensor readings, anima derivation | First | Lowest blast radius; Elixir-owned sensors mean any Python derived state must read from SHM. Once promoted, Python broker may continue to run in shadow but its sensor-reading code path is dead. |
| **B. Display+Face** | TFT display (SPI), face-state derivation, D22/D24 single-owner | Second | Visible regression if wrong. Phase B-display is gated on Phase B-sensors completing. |
| **C. Reflection+Telemetry** | Metacognitive reflection serialization, UNITARES telemetry forwarder | Third | Pure compute downstream of A+B. No hardware. Could go simultaneously with B. |

**Reserved hardware surface IDs (lease-plane interaction — addresses dialectic CONCERN; revised v0.2 for granularity consistency):**

The lease-plane RFC §3.3 reserves a `td:/op_path` row "for design fit" without registering. This RFC reserves analogous hardware IDs in the surface-ID schema, NOT registered with the Mac-side lease plane in v0. **All IDs are at device level (not role level) for consistency** — a future RFC can add role tags without renaming the surface IDs:

| Reserved ID | Purpose | Notes |
|---|---|---|
| `hw:/i2c/i2c-1` | I2C bus 1 ownership | If we ever register, the BMP280 wedge becomes a `held_by_other` event the moment any second process opens the bus — days-to-detect → seconds. |
| `hw:/gpio/D22` `hw:/gpio/D23` `hw:/gpio/D24` `hw:/gpio/D27` | Per-pin GPIO claims | All four pins the broker actually claims for joystick (D22/D24 also shared with TFT backlight/reset). Symmetric coverage prevents "we reserved only the contended pins" implying a different threat model than single-writer-to-hardware. |
| `hw:/spi/spidev0.0` | SPI device 0.0 (used by TFT) | Device-level (not role-level) so a future SPI-attached non-TFT device fits without renaming. |

The lease-plane `surface_id` schema (live-verified at `src/lease_plane/models.py:35` — `Field(min_length=1)`, no prefix constraint; DB CHECK constraints in `db/postgres/migrations/024_lease_plane.sql` do not constrain `surface_id` format) accepts any non-empty string, so the `hw:/` prefix is permitted by the existing schema. **No lease-plane RFC change required** for v0 reservation; future advisory registration may introduce a `surface_kind` enum entry, which is a one-row change to that RFC, not a re-design.

Wiring (round-tripping every hardware lease through the Mac across Tailscale) is correctly out of v0. **Schema reservation only.**

### 3.4 LED honesty (new in v0.1 — addresses dialectic BLOCK §3.2/§7.3)

v0 deferred LED ownership cleanup with the framing "stay split, it's its own RFC." Council pass 1 (dialectic) flagged this as cementing an unacknowledged invariant violation. The §2 corollary says "every hardware resource is owned by exactly one OTP process." LEDs are a hardware resource. The Python anima server holds the LED FD outside the BEAM supervision tree.

Stated honestly: **nothing structurally prevents an LED-class wedge during v0**. The single-writer-to-hardware discipline is convention, not enforcement, for LEDs specifically. It is the same shape as the BMP280 wedge before PR #45.

The v0.5 fold-in trigger: **any shared-bus-conflict-class symptom involving LEDs (LED FD held by two processes, or any duplicate-claim incident on the SPI/spidev0.0 bus the LEDs share with TFT) between Phase A and Phase C forces LED ownership into v0.5 before cutover**. Operator may also fold LED in voluntarily; this is not a delay until "after cutover." (v0.1 said "I2C-conflict-class" — corrected to "shared-bus-conflict-class" since LEDs are SPI, not I2C.)

This is not refusing scope creep — it is naming the deferred wedge so the v0 promise is honest about what it does and does not structurally fix.

## 4. Architecture

### 4.1 Supervision tree (revised v0.1 — split I2C from SPI per code-reviewer CONCERN)

```
AnimaBroker.Application
└── AnimaBroker.Supervisor (one_for_one)
    ├── AnimaBroker.I2CHardwareSupervisor (rest_for_one)
    │   ├── AnimaBroker.I2CBus           (owns /dev/i2c-1; first child)
    │   ├── AnimaBroker.BMP280
    │   └── AnimaBroker.IMU
    ├── AnimaBroker.SPIHardwareSupervisor (rest_for_one)
    │   ├── AnimaBroker.SPIBus           (owns /dev/spidev0.0; first child)
    │   └── AnimaBroker.TFTDisplay       (uses GPIOBus for D22/D24 + SPIBus)
    ├── AnimaBroker.GPIOSupervisor (rest_for_one)
    │   ├── AnimaBroker.GPIOBus          (owns BCM pin claims; first child)
    │   └── AnimaBroker.Joystick         (uses GPIOBus for D22/D23/D24/D27)
    ├── AnimaBroker.Reflection           (top-level: not hardware; per dialectic NIT)
    ├── AnimaBroker.SHMWriter            (writes /dev/shm/anima_state.json envelope)
    ├── AnimaBroker.HealthEndpoint       (Bandit/Plug HTTP, bound 127.0.0.1)
    └── AnimaBroker.Telemetry            (HTTP forwarder to UNITARES audit channel)
```

**Restart strategy (revised v0.2 — addresses code-reviewer ack-pass BLOCK §4.1):** `rest_for_one` on **all three** hardware sub-supervisors, with the bus-owner GenServer as first child. Reasoning: every hardware-child holds a handle obtained from the bus-owner; if the bus-owner dies, the child's handle is stale (FD closed at OS level on owner death). v0.1 used `one_for_one` on SPI/GPIO to avoid cascading restarts from TFT/Joystick crashes back into SPIBus/GPIOBus — but that strategy broke the OTHER direction (bus crash leaves child with stale handle). `rest_for_one` correctly restarts only descendants of the dying child: SPIBus death restarts TFTDisplay; TFTDisplay death does NOT restart SPIBus. Symmetric structure across I2C, SPI, and GPIO trees; the v0 single-`HardwareSupervisor` rationale is preserved (children depend on bus health) without conflating buses with each other. An I2C bus wedge does NOT force a TFT restart because they live under different sub-supervisors at the top-level `one_for_one`.

### 4.2 Wire to Python anima-mcp server (revised v0.1 — addresses code-reviewer BLOCK §4.2 + live-verifier CONCERN)

v0 keeps the **same SHM wire** (`/dev/shm/anima_state.json` JSON envelope). Below is the explicit lock-parity contract:

- **Final file path**: `/dev/shm/anima_state.json` (Phase B/C) or `/dev/shm/anima_state_elixir.json` (Phase A shadow).
- **Lock file**: `/dev/shm/anima_state.lock` companion file. The Elixir writer MUST acquire fcntl LOCK_EX on this lock file (NOT on the data file, NOT via Erlang `:file.lock` — those don't interop). Reference: `shared_memory.py:_write_file` line 70 uses `filepath.with_suffix(".lock")` and `fcntl.flock(lock_fd, fcntl.LOCK_EX)`.
- **Temp file path**: `<final>.tmp` (matches Python's `filepath.with_suffix(".tmp")`).
- **Write sequence**: open lock file in `"a"` mode, fcntl LOCK_EX, write to temp file, `flush()` + `fsync()`, atomic `os.replace(temp, final)` (or NIF equivalent providing `rename(2)` semantics on the same filesystem), fcntl LOCK_UN, close.
- **Phase B "gating Python's writes"**: when a bundle is promoted, the Python broker's `SharedMemoryClient` instance is replaced with a no-op writer (does not touch lock file, does not write temp file, does not write final). Not a flag check inside `write()` — full replacement to eliminate any race between Python's lock-acquire and Elixir's. Backup channel `/dev/shm/anima_state_python_backup.json` is OUT — Phase B is decisively single-writer.
- **Startup `.tmp` orphan cleanup (revised v0.2 — addresses code-reviewer ack-pass CONCERN F4):** Elixir broker on startup MUST `unlink` any pre-existing `<final>.tmp` file alongside the existing `clear()` of the final file. Failure mode: broker dies between fsync and rename (mid-write); `.tmp` persists with fresh data, final still has last-good. Without `.tmp` cleanup, next startup may see a stale `.tmp` and behave unpredictably. Symmetric handling for both `anima_state.json.tmp` and `anima_state_governance.json.tmp` (the bridge owns the second; bridge startup cleans its own `.tmp`).

#### 4.2.1 SHM envelope: fields Elixir broker WILL populate in v0

Live-verified envelope at `stable_creature.py:1002-1088` has 15+ top-level keys. Elixir broker in v0 populates ONLY the subset directly derivable from hardware + face/reflection:

| Key | v0 Elixir? | Source |
|---|---|---|
| `timestamp` | YES | BEAM clock |
| `readings` | YES | sensor GenServers |
| `anima` | YES | derived from readings |
| `inner_life` (basic dimensions) | YES | derived from anima |
| `drive_events` | NO | Python `agency` module — out of scope |
| `eisv` | NO | Python `unitares-bridge` writes this via separate SHM key (see §4.2.2) |
| `governance` | NO | Python `unitares-bridge` writes this |
| `identity` | NO | Bridge writes it to `anima_state_governance.json` (revised v0.2 — addresses dialectic ack-pass BLOCK on broker-stale-snapshot of identity). Broker does NOT read identity from disk and does NOT pass it through `anima_state.json`. Server merges identity from the bridge's SHM channel. |
| `metacognition` | YES | reflection module |
| `learning`, `experiential` | NO | Python modules — out of scope |
| `agency_led_brightness` | NO | Python `agency` — out of scope |

#### 4.2.2 Server fallback when Elixir-not-populated keys are missing (revised v0.1 — addresses code-reviewer BLOCK §4.2/§3.3)

Critical: if Elixir broker writes SHM without a `governance` key, the server's `SERVER_GOVERNANCE_FALLBACK_SECONDS=240s` timer triggers and the server begins calling UNITARES directly — re-introducing the pre-PR-#45 architecture violation. Live-verified at `server_state.py:58-59` (both threshold constants exist) + `server.py:948-966` + `loop_phases.py:23-47` (`_server_governance_fallback()` calls UNITARES directly).

Resolution: **the `unitares-bridge` Python sidecar (§2.1) writes a parallel SHM file `/dev/shm/anima_state_governance.json`** with `{governance, eisv, identity, drive_events, learning, experiential, agency_led_brightness, last_decision}`. Server's read path is updated to merge this side-channel into the data dict before the staleness check. Both files are written through their own lock files (`<final>.lock` per §4.2 contract).

**Two-file cross-freshness contract (revised v0.2 — addresses code-reviewer ack-pass BLOCK F1):**

| File | Threshold constant | Trigger |
|---|---|---|
| `anima_state.json` (broker) | `SHM_BROKER_STALE_SECONDS = 30s` | Broker writes every ~2s; stale after 30s = broker has died or wedged |
| `anima_state_governance.json` (bridge) | `SHM_BRIDGE_GOVERNANCE_STALE_SECONDS = 210s` | Bridge writes every ~180s; stale after 210s (matches existing `SHM_GOVERNANCE_STALE_SECONDS`; same constant repurposed) |

Server return shape per state:

| Broker SHM | Bridge SHM | Server returns to MCP callers |
|---|---|---|
| Fresh | Fresh | Normal full state |
| Fresh | Stale | `governance: degraded` (sensor data still served; governance/EISV/identity flagged stale) |
| Stale | Fresh | `hardware: degraded` (governance still served from last bridge tick; sensor data flagged stale) |
| Stale | Stale | `degraded: full` (both flagged; server still serves last-known on best-effort basis) |
| Either missing | — | `degraded: file_missing` with named-channel field |

**Server-side change scope (revised v0.2 — addresses code-reviewer ack-pass CONCERN F5; v0.1 framing of "one-line change" understated):** The actual server-side change is **multi-site, not one-line**. Live trace:
1. `loop_phases.py:23-47` — `_server_governance_fallback()` is removed (not "kept but bypassed").
2. `server.py:948-966` — fallback conditional block (`SERVER_GOVERNANCE_FALLBACK_SECONDS` arm) is replaced with typed-absence return shape per the table above.
3. `server.py:94` — import of `_server_governance_fallback` removed.
4. `server_state.py:58-59` — `SHM_GOVERNANCE_STALE_SECONDS` repurposed as `SHM_BRIDGE_GOVERNANCE_STALE_SECONDS`; `SERVER_GOVERNANCE_FALLBACK_SECONDS` constant deleted; new constant `SHM_BROKER_STALE_SECONDS = 30s` added.
5. Downstream callers of `governance_decision_for_display` updated to handle the typed-absence shape (display loop, health endpoint).

This is a moderate-scope refactor (5 sites). **It is the v0 deliverable** (§7.4 frame), with verification gated as a pre-Phase-A spike requirement (§9.3 frame). The two frames are not in tension once "v0 deliverable" is read as "must ship before v0 declares done" and "spike requirement" is read as "verified during the spike." The tracker for this work is §9.3's "Server fallback path verified" item.

This makes the server's fallback path explicitly: typed-absence (per lease-plane RFC §4.5 pattern) — when either SHM file is stale/missing, server reports the appropriate degradation flag to its own callers, NOT direct UNITARES call. The v0.1 decision FORECLOSES the v0 §7.4 option (c) — see §7.4 below.

#### 4.2.3 Other SHM channels (live-verifier CONCERN)

- `/dev/shm/anima_social_boost` — server writes it on user interaction; broker reads (live-verified at `stable_creature.py:547`, `communication.py:12`). Elixir broker MUST also read this flag. **Staleness model (revised v0.2 — addresses dialectic ack-pass BLOCK on social-boost):** treated as best-effort advisory boolean; **broker treats flag as `unset` if file is missing OR mtime older than `SOCIAL_BOOST_STALE_SECONDS = 10s`**. Lock-free is intentional (single-writer/server, single-reader/broker, advisory boolean — no atomicity-sensitive payload). The lock-free pattern is the explicit exception to the §4.2 lock-parity contract; named here so reviewers don't read it as a regression. Server's social-boost write is fire-and-forget; if server crashes between `open` and `write`, the file is left in an inconsistent state and the broker will fail-closed (treat as unset on parse error). Phase A divergence comparator MUST factor social-boost-applied state — see §6.1 row added.
- `~/.anima/display_brightness.json` — renderer writes brightness preset; broker reads each tick (live-verified at `renderer.py:119`, `stable_creature.py:500`). Same passthrough behavior.

### 4.3 Hardware ownership lines

Every hardware resource is owned by exactly one OTP process (subject to the §3.4 LED honesty caveat). FD lives in BEAM VM; direct hardware access from outside the supervision tree is OS-level prevented while BEAM is alive.

**Phase A two-reader caveat (code-reviewer CONCERN):** Phase A intentionally re-introduces a two-reader-on-I2C situation (Python broker for canonical reads, Elixir broker for shadow reads). Both processes use **read-only** semantics during Phase A — no concurrent writes to I2C, no bus resets from Elixir, no GPIO claim contention (Elixir reads sensor pins only, does not touch shared-pin pull-ups). This is the BMP280 wedge shape *deliberately* re-introduced for shadow comparison; it is bounded in scope (read-only) and time (1-2 weeks of Phase A).

**BEAM-down failure mode (foreclosed in §7.4):** when BEAM is down, the FDs are released. The Python server and `unitares-bridge` MUST NOT re-acquire them. Server-side discipline: stale-SHM beyond threshold → typed-absence to callers, NOT direct hardware read. This is a v0.1 server-side commitment, NOT a hope.

## 5. API surface (v0)

The broker exposes:

- HTTP `/health` on `127.0.0.1:<port>` — same shape as today.
- HTTP `/sensors` on `127.0.0.1:<port>` — diagnostic curl only; bound local.
- SHM write to `/dev/shm/anima_state.json` (or `_elixir.json` in Phase A).
- Telemetry POSTs to UNITARES audit channel.

The `unitares-bridge` Python sidecar exposes:
- HTTP `/health` on `127.0.0.1:<bridge-port>`.
- SHM write to `/dev/shm/anima_state_governance.json`.

No new public API in v0. The point of v0 is to swap the runtime, not the contract.

## 6. Rollout (shadow → swap → cutover)

### 6.1 Phase A — Shadow (week 1-2; revised v0.1 — addresses code-reviewer BLOCK §6.1)

- Elixir app runs alongside Python broker on the Pi.
- Reads sensors at the same cadence; writes shadow SHM `/dev/shm/anima_state_elixir.json`.
- Telemetry emits diff between Python and Elixir envelopes; UNITARES audit ingests.

**Concrete promotion gate (per surface_kind):**

| Field class | Diff metric | Threshold for promotion to Phase B |
|---|---|---|
| Sensor floats (temperature, pressure, accel) | `abs(python - elixir)` per field | ≤ 1% relative for ≥ 99% of samples over rolling 7-day window |
| Sensor ints (counters, raw GPIO) | byte-equal | exact, ≥ 99.9% of samples |
| Anima/derived (computed from sensors) | symmetric structural diff | ≤ 0.5% per-key disagreement after sensor-tolerance pass |
| Structural keys (presence) | exact set | exact match (no missing or extra keys) |
| Timestamps (`timestamp`, `updated_at`) | EXCLUDED | not compared |
| Reflection text | EXCLUDED | not compared (subjective; out-of-band review) |
| Anima during social-boost window | EXCLUDED for ±5s | revised v0.2 — addresses code-reviewer ack-pass DRIFT §4.2.3 vs §6.1. Social-boost flag (`/dev/shm/anima_social_boost`) read at different cadences by Python and Elixir produces phantom anima diffs; comparator drops the 10s window before/after each social-boost flag transition. |

**ASR baseline:** 7-day rolling pre-shadow measurement of the dashboard's `meta_existence_ratio` (alive_ratio) panel. Phase B promotion requires NO statistical drop (Welch's t-test p > 0.05) over 7-day shadow window vs. 7-day pre-shadow baseline.

**Regression triggers** (any one halts Phase A → B promotion):
- `audit.tool_usage` row with `error_type='shm_parse'` and `agent_id` matching the anima server (live-verified field name; the table has `error_type text` and `payload jsonb` columns; signal lives in `error_type`). **NEW INSTRUMENTATION REQUIRED**: live-verifier confirmed the anima server has zero rows in `audit.tool_usage` today. Adding the write path is a §9.3 spike deliverable, not an existing observable.
- `meta_existence_ratio` drop > 1 standard deviation from pre-shadow baseline for > 12h.
- Any operator-flagged hardware-anomaly KG entry tagged `lumen` (broad) OR `lumen-broker-port` (specific to this work — canonical exact-string tag, see §9.4 tag canonicalization) during the 7-day window.

### 6.2 Phase B — Swap (week 3-5; revised v0.1)

- Per **bundle** (not per surface), Elixir writes the canonical SHM file. Python broker's `SharedMemoryClient` is replaced with a no-op writer for that bundle's fields; Elixir is sole writer for that bundle going forward.
- Bundle order: A (Sensors+Anima) → B (Display+Face) → C (Reflection+Telemetry).
- Bundle B and C may proceed in parallel after A is stable.

**Promotion gate per bundle:**
- ≥ 3 days post-swap with NO regression triggers from §6.1.
- ≥ 3 days with no operator KG entry tagged `lumen-broker-port` AND severity ≥ medium.
- Server error log (from anima.service journalctl) shows zero entries containing `shm_parse` or `governance: degraded` over the window.

**Rollback (per bundle):** Python broker's SharedMemoryClient is restored. Elixir broker is reverted to shadow mode for that bundle. Rollback is per-bundle, not all-or-nothing. **Bundle B/C cannot start until Bundle A is stable** because Display+Face depends on Sensors+Anima as input.

### 6.3 Phase C — Cutover (week 6; revised v0.2 — live-verifier + dialectic ack-pass)

**Service file inventory (after Phase C):**

| Unit | Status | Purpose |
|---|---|---|
| `anima-broker.service` | Current; renamed to `anima-broker-py.service` and disabled at cutover | Old Python broker; archived (not deleted) for one release cycle |
| `anima-broker-elixir.service` | New | The Elixir broker (replaces the Python broker on the `PartOf=` chain) |
| `unitares-bridge.service` | New | Python sidecar; EISV mapping + UNITARES check-in (lifted from `stable_creature.py:567,925-998`) |
| `anima.service` | Existing; `PartOf=` rewired | MCP server + LEDs (unchanged process); now `PartOf=anima-broker-elixir.service` |
| `anima-broker-failed.service` | Existing | 60s cool-down on broker failure (live-verified) |

**`unitares-bridge.service` sketch (revised v0.2 — addresses dialectic ack-pass CONCERN on bridge supervision):**

```ini
[Unit]
Description=UNITARES Bridge — EISV mapping + governance check-in
After=network.target anima-broker-elixir.service
Requires=anima-broker-elixir.service
PartOf=anima-broker-elixir.service

[Service]
Type=simple
User=unitares-anima
WorkingDirectory=/home/unitares-anima/anima-mcp
ExecStart=/home/unitares-anima/anima-mcp/.venv/bin/unitares-bridge
Restart=on-failure
RestartSec=5
OnFailure=anima-broker-failed.service

[Install]
WantedBy=multi-user.target
```

`PartOf=anima-broker-elixir.service` makes the bridge restart whenever the broker restarts (clean-state coupling). `Requires=` ensures bridge won't start without broker. The bridge does NOT have a `PartOf=` from `anima.service` because `anima.service` reads bridge SHM but does not own its lifecycle.

**Ordered systemd transcript for cutover (revised v0.2 — addresses dialectic ack-pass CONCERN on PartOf= atomicity gap):**

```bash
# Pre-flight: Elixir broker + bridge units installed and validated independently
systemctl status anima-broker-elixir.service unitares-bridge.service
# Both should be: loaded, inactive (disabled but ready)

# Step 1: Stop the existing Python broker (anima.service stops too via PartOf=)
sudo systemctl stop anima-broker.service

# Step 2: Disable old broker
sudo systemctl disable anima-broker.service

# Step 3: Edit anima.service.d/override.conf to replace PartOf=
#   PartOf=anima-broker-elixir.service
sudo systemctl edit --full anima.service  # or write override file

# Step 4: Reload systemd to pick up edited unit
sudo systemctl daemon-reload

# Step 5: Enable + start the new broker (bridge auto-starts via Requires=)
sudo systemctl enable --now anima-broker-elixir.service
# unitares-bridge.service starts automatically via its Requires=anima-broker-elixir

# Step 6: Start anima.service (now bound to the new broker)
sudo systemctl start anima.service

# Step 7: Verify
systemctl status anima-broker-elixir.service unitares-bridge.service anima.service
```

The fragility v0.1 left implicit (changing `PartOf=` requires `daemon-reload` and does not retroactively apply to running units) is now explicit in the transcript: stop everything → reload → start in dependency order.

**Other Phase C steps:**
- `stable_creature.py` archived to `_archive/` (per repo convention; live-verified `_archive/` is the convention from `~/projects/_archive/schmidt-proposal-figures/` precedent).
- Python `unitares-bridge` sidecar STAYS (it carries the EISV/governance code).
- LED ownership verdict re-checked (§3.4 trigger): if any LED-class wedge symptom appeared during Phase A/B, fold-in happens here BEFORE cutover; otherwise carve-out persists into v0.5.

## 7. Open RFC questions

### 7.1 Nerves vs. vanilla Elixir on Raspbian — CLOSED in v0.1; remains CLOSED within this RFC in v0.3 (separate-RFC-conditional)

**v0.1 closed:** vanilla Elixir on Raspbian for v0. Trigger to re-open as a separate Nerves-migration RFC: **second Pi added to the fleet**.

**v0.3 update (post-S1):** S1 measured Elixir release idle RSS at 124 MB on Trixie (§8.2). Nerves builds a stripped BEAM image and would plausibly land closer to bare-ERTS (52 MB) — **but v0.3 does NOT re-open Nerves within this RFC's decision space**. The S1 result is not in itself a trigger to switch substrates. Nerves remains a *separately conditional RFC*: it opens only if (a) S6 falsifies AND (b) the operator explicitly authorizes reframing the project as "Lumen becomes an appliance OS." That reframing carries A/B firmware updates, replacement of the existing Tailscale + systemd + backup-script management story, and a wholly different operational shape. v0.3 is not authorized to make that call, and a future v0.4 that proposes a Nerves switch in-band is NOT a legitimate use of v0's amendment process.

**Order of consideration (v0.3 operator directive, 2026-05-01):**

1. Run S6 (Python distribution falsifier) first — see §9.3.B v0.3.
2. If S6 falsifies, operator decides: (a) accept that v0's case is dead and go "Python + single-writer discipline + watchdog + packaging discipline" instead of porting, OR (b) explicitly authorize the appliance-OS reframing, at which point Nerves becomes a separate RFC.
3. Do NOT jump from S1's RSS finding directly to "switch to Nerves." That route confuses a substrate measurement with an operational-shape decision.

`circuits_i2c` / `circuits_gpio` / `circuits_spi` remain non-Nerves-exclusive; the substrate switch is real but bounded by the appliance-OS framing decision, not by §8.2 RSS alone.

### 7.2 SHM JSON envelope: keep, or migrate to typed format

**v0.1 stance:** keep JSON envelope for v0. v0 ships a JSON Schema file (§7.6) as the contract floor. Strict typing migration (Pydantic↔Ecto) is its own RFC.

### 7.3 LED hardware: stay split, or fold into v0

**v0.1 closes:** stay split, see §3.4 honesty section. Fold-in trigger named.

### 7.4 What if the Elixir broker is down? — CLOSED in v0.1

v0 listed three options with no recommendation. v0.1 chooses:

- **(a) Server serves stale SHM with typed-absence flag** — chosen.
- (b) Fail health check to UNITARES — server already does this via separate `governance: degraded` reporting on stale SHM; not the runtime fallback.
- (c) Fall back to direct hardware reads — **explicitly foreclosed**. This is the BMP280 wedge by another name.

Server-side change required for v0: the existing `SERVER_GOVERNANCE_FALLBACK_SECONDS=240s` timer that triggers direct UNITARES call must be REPLACED with a typed-absence path (return `governance: degraded` to MCP callers, never direct call). This is part of the v0 deliverable, not deferred. **Without this, the §4.3 "structurally prevented" claim is false; with it, Elixir-down is bounded in failure mode.**

### 7.5 Hot-reload — out of scope for v0; restart cost named (revised v0.1 — addresses dialectic CONCERN)

Hardware-owning GenServers are deeply stateful (FD, calibration, peripheral handshake). Hot-reload is NOT a v0 promise. Supervisor restart IS the upgrade story. **Realistic restart cost:** I2C bus + sensors restart takes ~100ms-300ms (handshake + first-read settle); TFT restart takes ~500ms-1s (display init + first frame). During a deploy mid-tick, expect:
- 1-2 telemetry tick gaps (broker writes paused for restart window).
- Possible 1-tick mood-momentum dip in face state.
- A deploy during a governance-critical window (Mac side observing tight-margin verdicts) can produce a `stuck/critical_margin_timeout` event on the Mac.

**Deploy procedure** must therefore wait for Mac-side governance idle before broker restart, OR be coordinated through the lease plane (Mac-side broker holds a `surface:/lumen-deploy-window` lease that other agents observe). The lease-plane integration is OUT of v0; v0's deploy procedure is "manual coordination — operator chooses deploy window."

### 7.6 Cross-language schema source-of-truth — closed in v0.2 (per code-reviewer ack-pass + live-verifier DRIFT)

**v0 floor:** ship a JSON Schema file at `unitares/docs/schemas/anima_state_envelope.v0.json` (revised v0.2 — picks unitares repo because the RFC lives there and the schema is governance-cross-fleet, not anima-mcp-only; live-verifier confirmed `docs/schemas/` does not exist in either repo today, so `unitares/docs/schemas/` is the chosen home and creating it is a §9.3 spike deliverable).

**Strictness mode (revised v0.2):** `additionalProperties: true` on the top-level envelope (allow extra keys; Python broker writes 15+ keys including out-of-Elixir-scope ones); `additionalProperties: false` on each named sub-object schema (strict within the keys we DO define). This permits Phase A divergence comparator to ignore unknown top-level keys (Elixir omitting `learning`, `experiential`, etc. is permitted by schema) while catching typos within `readings.*` or `anima.*`.

**Versioning policy:** schema is versioned in the filename (`v0.json`, `v1.json`). Adding a key in v1 requires a new schema file; comparator validates each side against its declared schema version. Phase A comparator runs both v0 and v1 schemas if both Elixir and Python emit envelopes claiming different versions during a transition.

**Validator equivalence (revised v0.2 — addresses code-reviewer ack-pass CONCERN F12):** Python uses `jsonschema` with `format_checker=Draft202012Validator.FORMAT_CHECKER`; Elixir uses `ex_json_schema` with `format_validator: ExJsonSchema.Validator.FormatValidator` (opt-in). Both libraries differ on:
- `format` keyword: Python validates by default; Elixir is opt-in. v0 explicitly opts both into format validation.
- Regex flavor: Python `re` vs Elixir `Regex` (PCRE-compatible) — schema avoids regex constructs that differ between flavors (no lookbehinds, no Unicode property escapes; only basic `pattern` constraints).

**Corpus contract test (revised v0.2):** `unitares/tests/test_anima_state_envelope_schema.py` ships with v0 — a fixture corpus of 50+ recorded envelopes (live captures from broker SHM during shadow phase) validated against the schema by both Python and Elixir. Test fails if validators disagree on any corpus entry. This converts "both sides validate" from convention to contract.

Generated bindings (Pydantic↔Ecto) deferred. Validation is a contract test, not a runtime gate.

### 7.7 D22/D24 refresh removability

**v0.1 stance:** removable in Phase B-display, after Elixir's `GPIOBus` becomes single owner. Verify in shadow phase that Elixir reads of D22/D24 don't observe pull-up droop without the periodic refresh hack. Currently a §9 checklist item.

## 8. Concerns / counter-arguments / minority views (revised v0.1 — addresses dialectic CONCERN)

### 8.1 "Python's been working. Why migrate?" (steelmanned in v0.1)

Stronger version: *"Five of six PRs landed in <30 days of operator-developer time. The empirical bug-arrival rate is decreasing — PR #45 was the architecture-class fix; the architecture is now consistent. You're proposing a 4-8 week port to prevent N future bugs of the same class, when the past N≤6 cost less than the port will."*

**Honest answer:** for the *backward-looking* fault count, this is correct. The argument relies on **forward-looking surface count**:
- Voice ownership cleanup is in the queue (dual-ownership today, live-verified).
- LED ownership cleanup is in the queue (§3.4 trigger).
- Mic/speaker hardware deduplication is in the queue.
- Lumen-as-appliance distribution (single Elixir release vs. Python+venv) is the load-bearing distribution argument; it does not depend on fault count. (v0.3: this leg is now gated on S6 — see §8.5 v0.3 and §9.3.B v0.3.)

Concession: the Pi-side architectural argument is **weaker than the Mac-side argument** (where 17+13 concurrency commits over 4+ months is harder to dismiss). The Pi-side case stands on (a) supervision-tree-as-recovery-story for items 2,3 of §1.1, plus (b) appliance-shaped distribution. Each alone is insufficient; the conjunction is.

### 8.2 "BEAM is heavy on a Pi 4." — FALSIFIED on Pi 4B / Trixie / OTP 27 (revised v0.3)

**v0.1/v0.2 stance (struck):** "Vanilla Elixir resident memory ~25-40 MB (NOT live-verified — no Elixir process running on Pi yet)."

**v0.3 measured facts (S1 spike, 2026-05-01, Lumen — Pi 4B Rev 1.5, Debian Trixie 13, aarch64, 4 GB; runtime banner: `Elixir 1.18.3 (compiled with Erlang/OTP 27)`, `[erts-15.2.7] [jit]`, 4 schedulers; Debian package versions per `dpkg -l`: `elixir 1.18.3.dfsg-1`, `erlang-base 1:27.3.4.1+dfsg-1+deb13u1`; installed via `apt install elixir` from Trixie main):**

| Process | VmRSS | RssAnon | VmLib | RssShmem | Threads |
|---|---|---|---|---|---|
| Bare ERTS (`erl -eval timer:sleep(infinity)`) | **52.1 MB** | 37.8 | 69.0 | 7.4 | 24 |
| Default `mix release` (mix new --sup + 1 idle GenServer) | **123.7 MB** | 86.0 | 70.7 | 30.5 | 24 |
| Default release + `strip_beams` + `+S 1:1 +MMscs 0` | **124.9 MB** | 87.2 | 70.7 | 30.5 | 18 |
| Python broker today (`anima-creature`, 2d21h uptime) | **76.7 MB** | 49.9 | 54.8 | 0 | 8 |

All measured via `/proc/<pid>/status` VmRSS, idle, stable across 30/60/120s readings. BEAM-internal `:erlang.memory().total = 52.5 MB` (queried on the running default release) — i.e. Erlang's own allocator accounting sees the release using essentially the same heap budget as bare ERTS, while OS-level VmRSS for that same release is 123.7 MB. The +71 MB gap is therefore invisible to `:erlang.memory()`: it is shared-library mappings, allocator carveouts, and BEAM file backing that show up in VmRSS but not in Erlang's heap sum. The gap decomposes as RssAnon (+48 MB) and RssShmem (+23 MB); reducing schedulers to 1 had **no measurable effect**, so it is **not per-scheduler arena cost** — it is the OTP boot script's preload set + Elixir runtime libs + initial allocator carveouts.

The 25-40 MB estimate likely came from x86_64 / smaller-OTP environments. On aarch64 / Trixie / OTP 27 / JIT, bare ERTS already lives at 52 MB; a default Elixir release adds another 70+ MB on top.

**S1 gate verdicts (per §9.3.B):**

| Gate | Threshold | Measured | Verdict |
|---|---|---|---|
| Tarball size | < 30 MB | 5.72 MB (default config: 6,000,680 B; strip_beams rebuild: 6,000,657 B; both round to 5.72 MB) | PASS ~5x margin |
| Cold-start | < 5 s | 1.42 / 1.45 / 1.61 s (3 runs) | PASS ~3x margin |
| Idle RSS | ≤ 40 MB go; > 60 MB falsifies | **123.7 MB** | **FALSIFIES** |

**Honest reading:** Elixir on this Pi clears the cold-start and tarball gates by comfortable margins, but loses the idle-RSS comparison even against the existing Python broker (Elixir release ~124 MB vs. live Python broker 77 MB, a +47 MB regression). The conclusion in v0.1/v0.2 — that the BEAM substrate is comfortably under-budget — does not survive contact with measurement. The §8.2 falsifier threshold is **not re-anchored after the fact**; the §8.5 conjunction is reworked instead (see §8.5 v0.3).

KG anchor: discovery `2026-05-01T09:29:02.864022+00:00` tag `lumen-broker-port` severity `high`.

### 8.3 "You'd be debugging hardware drivers in BEAM."

True, and not nothing. `circuits_i2c` / `circuits_gpio` / `circuits_spi` wrap the same Linux kernel devices Python uses. Driver semantics same; runtime around them changes. Real cost — flagged, not minimized.

### 8.4 "Why not Go?"

Same answer as lease-plane RFC §8.3: Go gives cheap concurrency but no supervision primitive. Mac+Pi BEAM unifies the substrate. KG `2026-04-30T19:30:54.644112+00:00` operator decision settles this.

### 8.5 "This is just substrate migration tax dressed as architecture." (revised v0.4 — S6 ran with ambiguous result; distribution leg quantitatively weaker than v0.3 framed)

Stronger version: *"The Pi-side incident class is single-host single-Pi single-process-pair. OTP's load-bearing wins are supervision-on-multi-process and cross-process coordination via mailboxes. Single-host single-Pi has neither — broker + server, two processes, coordinating via a 1KB JSON file. You don't need OTP to fix two processes coordinating via a JSON file; you need a contract test, an `lsof` check in CI, and a single-writer linter rule."*

**Honest answer (v0.1/v0.2 — unchanged):** the genuine OTP-shaped wins are items 2 (rest_for_one cascade for bus recovery) and 3 (single GenServer-owned GPIO). The other items in §1 are addressed by *any* substrate change with discipline. The OTP-specific value is **supervision-tree-as-recovery-story** as an ergonomic frame: explicit restart strategies, observable child trees, structured upgrade story. That is *one* well-formed argument, not five.

**v0.1/v0.2 conjunction (also unchanged in form):** the case for v0 was a **two-leg** conjunction — supervision-tree-recovery + appliance-shaped distribution. Per v0.2 explicitly: *"Each alone is insufficient; the conjunction is."* (§8.1) and *"The conjunction is what justifies v0; either alone does not."* (§8.5 v0.2). v0.3 does NOT re-enumerate this. Supervision-tree-recovery alone was already insufficient pre-S1; that has not changed.

**What S1 actually changed (v0.3 — narrow claim):** v0.1/v0.2 carried an *implicit positive prior* in §8.2: the assumption that "BEAM fits comfortably on the Pi" (the unverified 25-40 MB estimate). This prior was not an enumerated leg of the conjunction — it was background support that made the appliance-distribution leg easier to argue for (a single Elixir release tarball that fits in a small RSS budget is more obviously appliance-shaped than one that costs +47 MB over the existing Python broker). S1 falsifies that prior. The structural conjunction (supervision + distribution) is intact in form; what is gone is the assumed memory headroom that made the distribution leg cheap to argue for.

**Effect on each leg (revised v0.4 with S6 measurements folded in):**

- **Supervision-tree-recovery leg:** unchanged. Was already load-bearing-only-in-conjunction in v0.2; still is. **§1.1 items 2 + 3 named explicitly:** any retire-to-Python path leaves PR #11's hand-rolled `recreate I2C bus handle when multiple sensors fail` in place (no `rest_for_one` cascade) and leaves PR #14's periodic 30s D22/D24 OUTPUT-HIGH refresh hack in place (no single GenServer GPIO owner). These are bugs the OTP-shaped-win argument was meant to fix structurally; "Python + discipline" leaves them as-is. Trade-off named, not papered over.
- **Appliance-distribution leg:** **weaker than v0.3 framed it, but the ≥70% / ≤20% gate is qualitatively ambiguous (50-75% by sub-axis weighting).** S6 result (KG `2026-05-01T11:32:43.014879+00:00`, supersedes withdrawn `11:02:42`):
  - **Apples-to-apples sizes (MiB, 1024-division, byte counts in parens):** realistic Elixir release with hardware deps (circuits_i2c/gpio/spi + bandit + plug + jason; no domain code yet) = **7.262 MiB** (7,614,904 B); Python+shiv broker (verified runnable end-to-end) = **43.346 MiB** (45,451,738 B). Ratio: 5.97x Elixir favor on tarball; 11.6x Elixir favor on after-deploy disk (shiv extracts 177 MiB cache to `~/.shiv/<hash>/` on first run, total ~220.3 MiB).
  - **Idle VmRSS:** realistic Elixir release = 143.4 MiB; Python broker live = 73.0 MiB. **Python wins runtime memory by +70.4 MiB** (vs +47 MiB v0.3 estimated against the hello-world skeleton).
  - **Sub-axis scoring of distribution win (shiv vs realistic Elixir):** single-artifact deploy 100% (both ✓); self-contained on bare distro 50-70% (shiv requires apt python3, pinning deploy to Debian-family ecosystems — real concession, not "close match"); size economy ~17% (5.97x bigger tarball, 11.6x bigger footprint); operational shape (systemd + Restart=on-failure + watchdog) equivalent post-port, NOT a Python lead today (correcting prior status-quo bias). **Aggregate 50-75% — within axis-weighting variance of the 70% gate** (the range reflects how the operator weights size economy / ecosystem-pinning / single-artifact-shape; not measurement uncertainty).
  - **Cost trade:** "Python + shiv + lsof CI + docs" estimated 3-5 days vs BEAM port 4-8 weeks (20-40 days). Ratio 7.5-25%. Clears ≤20% at lower end, brushes at upper end. Caveat: no Pi-native CI runner exists today, so the lsof CI hook must be built against a self-hosted Pi runner OR mocked-only.
  - **Nuitka was NOT measured.** Build was killed at 43:21 elapsed on Pi 4 (still mid-gcc compile of 1048 modules); Pi-side build time is the wrong axis (real release pipelines build on dev machines and ship binaries). A completed Nuitka build (~80-120 MiB single binary, no system-Python dependency) would shift "self-contained" to full and likely push Python's aggregate above 70%. Resolving Nuitka would resolve the ambiguity.

**Per §9.3.B v0.3 decision tree, this S6 result is the AMBIGUOUS branch.** Tree's default posture: "treat as falsified" — bar for v0 to continue is positive evidence. v0.4 does NOT pre-decide on the operator's behalf — §8.5 v0.3 line 569 ("the leg menu is operator-supplied, not pre-supplied by v0.3") still binds.

**Default-posture divergence acknowledgment (v0.4):** v0.3's "treat as falsified" reads literally as "v0 collapses, retire-direction." v0.4 operationalizes this by holding S2-S5 in PAUSED state pending explicit operator retire-or-resume action — one step softer than literal retirement. The reasoning: a draft RFC mid-flight cannot mechanically self-retire on an ambiguous-but-not-clearly-falsifying measurement; the literal "treat as falsified" reading would require the spike author to declare v0 dead unilaterally, which conflicts with the operator-supplied-decision rule. The operationally-coherent reading is "default to retire unless operator overrides, but hold the structural state (paused S2-S5, paused §9.3.A items) until that decision lands so neither retire nor resume is foreclosed by amendment timing." Flagged here for ack-pass scrutiny — operator may correct to literal-retire-default if preferred.

**Epistemic paths the operator may consider to resolve the ambiguity** (non-exhaustive — these are *ways to disambiguate*, NOT a closed list of operator decision branches; per §8.5 v0.3 the leg menu is operator-supplied):

1. Complete Nuitka build on a dev machine (Mac aarch64 cross-compile or ARM cloud builder; ~1-2h wall-clock if Adafruit cross-compile works) — gives the missing Python+Nuitka data point.
2. Operator weights the qualitative axes (size economy / ecosystem-pinning / single-artifact-shape) explicitly — 70% verdict resolves deterministically.
3. Operator invokes an additional load-bearing leg per §8.5 v0.3 (substrate-uniformity-with-Mac-lease-plane or any other; the hedge "depending on the leg's mechanism" — some legs may make S2-S5 actionable; others may re-frame v0 independent of S6) — re-frames v0 independent of S6.

## 9. Pre-implementation checklist (revised v0.1 — addresses §9 BLOCKs from dialectic and live-verifier)

### 9.1 Lease-plane substrate status (live-verifier BLOCK)

- **Lease plane schema:** DEPLOYED (migration `024_lease_plane.sql` applied; `lease_plane.surface_leases` and `lease_plane.lease_plane_events` exist in governance DB; live-verified).
- **Lease plane Elixir process:** NOT RUNNING (port 8788 connection refused; 0 rows in both lease tables; live-verified).
- **This RFC's Phase A is NOT gated on lease plane runtime health.** Lease plane is the operator-decision-and-substrate-test for "BEAM on the fleet"; it is NOT a runtime dependency for the Pi broker port. Anima broker BEAM port can begin Phase A independently of lease plane reaching Phase B.
- A future v1 RFC can integrate the broker with lease plane for `hw:/` advisory leases (§3.3 reservation); v0 does not.

### 9.2 Council pass items

- [ ] §7.1 Nerves vs. vanilla — CLOSED in v0; SEPARATE-RFC-CONDITIONAL on S6 + appliance-OS framing (does NOT re-open Nerves within this RFC; v0.3 keeps the door for a future separate Nerves-port RFC, not for an in-band v0.4 substrate switch)
- [ ] §7.2 SHM envelope schema — JSON Schema in v0 (§7.6); typed migration deferred
- [ ] §7.3 LED scope — CLOSED in v0.1 (§3.4 honesty + trigger)
- [ ] §7.4 down-mode behavior — CLOSED in v0.1 (option a; option c foreclosed)
- [ ] §7.5 hot-reload — CLOSED out of v0; restart cost named
- [ ] §7.6 cross-language contract — JSON Schema floor

### 9.3 Pre-Phase-A work (revised v0.2.1 — split into production deliverables vs. spike experiments; the original §9.3 list mixed these and obscured the value of the spike)

The original v0.2 §9.3 mashed together production code that must ship before Phase A with experiments meant to *discover* unknowns. v0.2.1 splits these into two clearly-labeled subsections so each gets the right shape: deliverables get owners and ship dates; spike experiments get hypotheses and discrete go/no-go gates.

#### 9.3.A Production deliverables (must ship before Phase A starts; v0.3: pacing split)

These are code/config changes with defined endpoints. Each must be shipped, tested, and visible in the running system before the divergence comparator turns on.

**v0.3 pacing split (post-S1):** §9.3.A originally treated all five deliverables as a single batch ("must ship before Phase A starts"). Post-S1, with v0's case gated on S6, two of the deliverables have value *independent* of the BEAM port and may proceed; the other three exist solely to support Phase A and pause until S6 holds.

**Continue (independent value, ship regardless of S6 outcome):**

- [ ] **Server fallback typed-absence path** — multi-site refactor per §4.2.2 (5 sites: `loop_phases.py:23-47`, `server.py:948-966`, `server.py:94`, `server_state.py:58-59`, downstream callers). `SERVER_GOVERNANCE_FALLBACK_SECONDS` direct-UNITARES code path **deleted, not bypassed**. New constant `SHM_BROKER_STALE_SECONDS = 30s`. Verified by integration test that triggers each of the 5 staleness states and asserts the §4.2.2 return-shape table. **Independent value: the existing Python broker also benefits — eliminates the §4.3 BMP280-wedge-by-another-name fallback path. Ship without waiting on S6.**
- [ ] **`audit.tool_usage` write path** instrumented in anima server. New code inserts row with `error_type='shm_parse'` on parse failure, and `tool_name=anima_broker_tick` for broker operational telemetry. Live-verifier confirmed the table is currently empty from server (0 rows). Tested with a synthetic SHM parse failure injection. **Independent value: broker telemetry stands on its own; the §6.1 regression-trigger query is reusable for any broker that writes the SHM envelope. Ship without waiting on S6.**

**Pause (Phase-A-only value, gated on S6 holding):**

- [ ] **JSON Schema file** at `unitares/docs/schemas/anima_state_envelope.v0.json`. `additionalProperties: true` at top level, `false` in named sub-objects (§7.6 strictness model). **PRECONDITION** for §9.3.A validator test below. **Pause: the schema's only consumer is the cross-language envelope contract; if S6 falsifies, there is no second writer to validate against.**
- [ ] **Cross-language validator corpus contract test** at `unitares/tests/test_anima_state_envelope_schema.py` — 50+ recorded envelopes from broker SHM, validated by both `jsonschema` (Python) and `ex_json_schema` (Elixir) with format-validator opt-in. Test fails if validators disagree on any entry. (See §7.6 for `format` and regex flavor caveats.) **Pause: same gate as the schema — no Elixir writer means no validator equivalence to enforce.**
- [ ] **`unitares-bridge.service` systemd unit** (per §6.3 sketch). Reads `anima_state.json`, computes EISV, writes `anima_state_governance.json`, posts to UNITARES. Includes `first_check_in` restart-state persistence at `~/.anima/unitares_bridge_state.json` (§9.4). **Pause: the bridge sidecar exists only to compensate for Elixir-broker-can't-call-UNITARES; if the port retires, today's `bridge.check_in()` in `accessors.py:982-989` continues unchanged.**

#### 9.3.B The Spike (revised v0.4 — S6 ran with ambiguous result; S2-S5 stay PAUSED per default-treat-as-falsified)

**v0.4 status (2026-05-01):** S6 ran on Lumen post-v0.3 ack-pass. **Result: ambiguous on the 70% distribution-win gate.** First S6 verdict (KG `2026-05-01T11:02:42`) was withdrawn after a 3-agent council found fatal evidence gaps (broken shiv artifact, hello-world Elixir comparison, killed Nuitka treated as failure, pre-supplied operator branch menu). Re-measurement with corrections produced KG `2026-05-01T11:32:43`: 50-75% of the appliance-distribution win at 7.5-25% of the BEAM-port cost — qualitatively ambiguous, within axis-weighting variance of the 70% gate. See §8.5 v0.4 for the full table. Per the §9.3.B v0.3 ambiguous-default-treat-as-falsified posture, v0.4 holds S2-S5 in PAUSED state pending operator decision (one operational step softer than literal retire-default; see §8.5 v0.4 closing paragraph for the divergence acknowledgment).

**v0.3 status (preserved for context):** S1 ran on Lumen and falsified the §8.2 RSS estimate (see §8.2 v0.3). Per operator directive, the spike does NOT continue under the v0.2.1 decision tree. S6 (Python distribution falsifier) was promoted to next-up and ran *before* S2-S5 resume. v0.3 does NOT swap to Nerves; that remains a named option behind a separate operator decision (see §7.1 v0.3).

**Original framing (still valid):** the spike's job is to make us *know what we don't know* before committing 4-8 weeks to the full port. Cheap experiments with measurable gates; each names a falsifier; failed gates halt the spike and force a versioned amendment with the finding folded in.

**Revised gate table (v0.3):**

| # | Experiment | Status | Result / Notes |
|---|---|---|---|
| **S1** | Cold-start sanity | **DONE 2026-05-01** | Tarball 5.72 MiB PASS; cold-start 1.4-1.6s PASS; idle RSS **123.7 MiB FALSIFIES** §8.2 (>60 MiB). Apples-to-apples comparison set in §8.2 v0.3. KG `2026-05-01T09:29:02.864022+00:00`. |
| **S6** | Python distribution falsifier | **DONE-AMBIGUOUS 2026-05-01** | shiv pyz built and verified runnable end-to-end (43.346 MiB single artifact; ~220.3 MiB total after first deploy with extraction cache); realistic Elixir release with hardware deps = 7.262 MiB tarball / 19 MiB unpacked / 143.4 MiB idle VmRSS. shiv vs Elixir: 5.97x size penalty / 11.6x footprint penalty / Python +70.4 MiB memory advantage. Aggregate distribution-win 50-75% (qualitatively ambiguous on the 70% gate, within axis-weighting variance); cost ratio 7.5-25% (clears ≤20% at low end, brushes at high end; ratio excludes one-time Pi-native CI runner setup since none exists today — see §8.5 v0.4). Nuitka NOT measured (build killed at 43:21 mid-gcc on Pi 4; Pi-side build time is the wrong axis — real release pipelines build on dev machines). KG `2026-05-01T11:32:43.014879+00:00` supersedes withdrawn `11:02:42`. v0.3 ambiguous-default routes "treat as falsified" — v0.4 holds S2-S5 in PAUSED state pending operator decision (one operational step softer than literal retire-default; see §8.5 v0.4 for the divergence acknowledgment). |
| **S2** | BMP280 GenServer | **PAUSED** | Stays paused under v0.4. Per the v0.3 ambiguous-default-treat-as-falsified posture, the bar for resumption is positive evidence — operator-supplied. See §8.5 v0.4 for epistemic paths the operator may consider to resolve the ambiguity (measurement / axis-weighting / additional leg, non-exhaustive); operator decides which, if any, to take. |
| **S3** | SHM lock parity | **PAUSED** | Same gate as S2. |
| **S4** | Supervisor cascade | **PAUSED** | Same gate as S2. |
| **S5** | Bridge stub + typed-absence | **PAUSED** | Same gate as S2. |
| **S7** | Phase A divergence comparator dry-run | **PAUSED** | Depends on S2-S5; same gate. |

**Spike methodology notes:**

**v0.3 (carried forward):** during S1 cleanup, `pgrep -f beam.smp` repeatedly matched the SSH bash session whose own command line contained the literal string `beam.smp` (from the script being executed), creating the illusion of a respawning BEAM process. Future spike checks against the BEAM substrate must use stricter process matching:

- Prefer `pgrep -x beam.smp` (exact basename match) over `pgrep -f`.
- When `-f` is required (e.g., to disambiguate by command-line args), inspect `ps -o pid,ppid,cmd` output explicitly and exclude self / parent shell PIDs.
- For BEAM-specific lookups, trust the release's own `bin/<app> pid` over pgrep — the release writes a pid file at `<rel>/tmp/pids/<app>.pid` that is authoritative.

**v0.4 (S6 lessons):** the first S6 verdict was withdrawn after a 3-agent council found four classes of evidence/framing errors. Future bundling-comparison spikes must:

- **Compare apples-to-apples.** A `mix new --sup` skeleton tarball is NOT a fair comparison against a full Python broker shiv pyz. For S6, the realistic Elixir baseline must include the hardware-dep substrate (circuits_i2c/gpio/spi + bandit + plug + jason) that any actual port would compile in. Domain code adds KBs to low-MBs on top; the deps are the bulk.
- **Verify runnability, not just import.** Timing `python3 -c "import X"` proves nothing about whether the bundled artifact actually executes end-to-end. Use a graceful-shutdown trace (timeout + SIGTERM, watch for shutdown sequence) to confirm the entry-point reaches main() and subsystems initialize.
- **Don't conflate "killed" with "failed."** A long-running build that the spike author terminates is "not measured," not "failed." Report it as such in the verdict and route through the appropriate decision branch ("ambiguous" or "operator escalation"), not through the "falsifies" branch.
- **Honor §8.5's leg-menu-is-operator-supplied rule.** When the spike outcome is ambiguous, the verdict author MUST NOT enumerate the operator's possible decision branches as a closed list. Future spike verdicts that encounter the ambiguous branch should explicitly cite §8.5 v0.3 line 569 and decline to pre-decide.

These v0.4 lessons apply to S2-S5 + S7 if they ever resume, and to any future versioned spike (e.g., S8+ if added).

**v0.3 spike outcome decision tree (with v0.4 status annotations):**

- **S6 falsifies (Python-discipline path wins on the 70%/20% trade):** v0 case collapses. v0 retires; v0.5 closes the RFC with "Python + discipline" as the chosen path, OR operator explicitly authorizes the appliance-OS reframing (§7.1) which would open a separate Nerves-port RFC, NOT continue v0. (A third path — operator invokes a non-pre-supplied additional load-bearing leg per §8.5 v0.3 — is permitted but not pre-mapped here; it would require an explicit v0.5 amendment naming the new leg.) [v0.4: not the current branch.]
- **S6 holds (no Python-discipline path delivers comparable distribution win):** distribution leg survives. v0.5 council ack-pass, then S2-S5 resume. The +70.4 MiB memory regression (revised from v0.3's +47 MB estimate against the hello-world skeleton) is acknowledged as a tolerated cost, not papered over. [v0.4: not the current branch.]
- **S6 ambiguous:** operator decides. Default posture is "treat as falsified" — the bar for v0 to continue is positive evidence, not absence of negative evidence. **[v0.4: THIS IS THE CURRENT BRANCH.** S6 ran with aggregate distribution-win 50-75% / cost ratio 7.5-25%. Default-treat-as-falsified holds: S2-S5 stay PAUSED. Operator decision required to break ambiguity per the three resolution paths in §8.5 v0.4.**]**

**S2-S5 + S7 failure mapping (post-S6-holds):** the v0.2.1 decision tree's per-experiment failure clauses are **NOT** carried forward implicitly under "original gate semantics." The v0.3 mapping below restates them inside the post-S6-holds branch:

- **S2 fails** (BMP280 read-latency / I2C error / handshake-on-reboot): hardware-library substrate doesn't deliver Python-parity. → v0.5 architectural revisit (likely retire; `circuits_i2c` was the OTP-shaped-win-as-libraries argument).
- **S3 fails** (torn writes on shared `anima_state.lock`): §4.2 lock-parity contract is broken; two-process SHM coordination doesn't survive Elixir as a writer. → v0.5 architectural revisit (likely retire — kills cross-language SHM strategy).
- **S4 fails** (rest_for_one cascade timing / FD leak): §4.1 v0.2 supervisor-strategy was wrong despite ack-pass. → v0.5 amendment to §4.1 (not full retire — supervision strategy is fixable inside the BEAM port).
- **S5 fails** (two-file freshness table behavior deviates): §4.2.2 typed-absence model doesn't hold. → v0.5 amendment to §4.2.2 (also fixable inside the port).
- **S7 surfaces issues** (§6.1 thresholds trigger spuriously or miss real diffs): threshold-tuning v0.5 textual update.
- **All S2-S5 + S7 green post-S6-hold:** v0.5 empirical fold-in, Phase A starts.

The v0.2.1 decision tree (S1/S2/S3 fail → switch to Nerves OR back to Python) is **superseded by this v0.3 tree**. The Nerves-vs-Python decision is no longer triggered by individual gate failures; it is gated on (a) the operator's appliance-OS framing decision and (b) S6's distribution-leg verdict. Per-experiment failure paths above route to v0.5 (amendment or retire), not directly to a substrate switch.

### 9.4 Crash-recovery and edge cases (revised v0.2 — addresses code-reviewer + dialectic ack-pass CONCERNs)

- [ ] **Elixir broker startup behavior** — clear SHM on startup (matches Python broker's `shm_client.clear()` at `stable_creature.py:325`, live-verified). Also unlinks `<final>.tmp` orphan from any prior crash mid-write. Stale-from-pre-crash data is NEVER served as live state.
- [ ] **Bridge startup behavior** — `first_check_in = True` reset on bridge restart re-introduces "first check-in" semantics with UNITARES, potentially resetting circuit-breaker / agent session state. **Mitigation**: bridge persists last-check-in timestamp to `~/.anima/unitares_bridge_state.json` on each successful check-in; on startup, if file exists and timestamp is fresh (< 600s), the bridge sets `first_check_in = False` and uses the stored agent_uuid/parent_agent_id from that file. If file is missing or stale, fresh first check-in (per identity.md v2 fresh-instance posture). This makes bridge restart transparent to UNITARES while still honoring fresh-process-instance semantics across longer outages.
- [ ] **Bridge crash during shadow / Phase B** — Phase A divergence comparator continues with Elixir-only data (broker not affected). Server reports `governance: degraded` per §4.2.2 typed-absence table. Bridge auto-restart via `Restart=on-failure` + `RestartSec=5`; bridge re-reads `anima_state.json` on restart.
- [ ] **Python broker crash during shadow** — Phase A divergence comparator must tolerate Python broker dying (continue with Elixir-only data; flag as `python_unavailable`).
- [ ] **Hardware unavailable** — sensor disconnect handler returns `:error` from GenServer.call; SHM envelope shows `readings: {error: "unavailable"}`; server tolerates via `.get()` pattern.
- [ ] **Malformed SHM** — JSON parse fail in server log; v0 server change adds explicit `audit.tool_usage` row insertion with `error_type='shm_parse'` for the §6.1 regression trigger.
- [ ] **Rollback from partially promoted bundle** — v0 deploy procedure requires explicit per-bundle rollback test before promotion. Test: bundle A promoted in test environment → simulate Elixir crash → restore Python broker SHMWriter → verify server returns to normal mode.

#### 9.4.1 KG tag canonicalization (revised v0.2 — addresses code-reviewer ack-pass NIT F14)

The §6.2 promotion gate ("no operator KG entry tagged `lumen-broker-port` AND severity ≥ medium for ≥ 3 days") depends on a negative query. False negatives from typos would silently satisfy the gate. Canonicalization:

- **Canonical tag**: `lumen-broker-port` (exact, hyphenated, lowercase).
- **Tag schema enforcement** during Phase A/B: `audit.tool_usage` write path includes a tag-vocab check; the tag-validator emits a warning row if it sees `lumen_broker_port`, `lumen-brokerport`, or other near-misses, OR if a KG `discoveries` row tagged with a near-miss is observed.
- **Promotion gate query**: `SELECT * FROM knowledge.discoveries WHERE 'lumen-broker-port' = ANY(tags) AND severity >= 'medium' AND created_at >= NOW() - INTERVAL '3 days'`. Exact match.
- The lease-plane RFC's `surface-leases` tag was used informally; this RFC formalizes one tag with one spelling for the duration of the implementation.

### 9.5 Cross-link

- [ ] Cross-link with `surface-lease-plane-v0.md` Phase A status. **Concrete dependency direction (revised v0.2 — adds Phase C statement):** Pi RFC Phase A may proceed independently of lease plane Phase A; Pi RFC Phase B (swap) does not require lease plane in any specific phase; **Pi RFC Phase C (cutover) does not require lease plane runtime in any state**. NO phase of the Pi RFC has a runtime dependency on the Mac-side lease plane. The broker's `hw:/` advisory leases are reserved (§3.3) but not registered in v0; future v1 RFC may add registration as a strictly-additive change.

## 10. Versions / changelog

**Version ladder (revised v0.2 — addresses code-reviewer ack-pass CONCERN F9):**

| Version | Pass | Promotion gate |
|---|---|---|
| v0 | initial draft | — |
| v0.1 | council pass 1 amendments | NO-SHIP returned 3/3; v0.1 addresses 8 BLOCKs from pass 1 |
| v0.2 | ack-pass on v0.1 amendments | Addresses 7 new BLOCKs found in v0.1 amendments; council-clean |
| v0.2.1 | spike scope rescope (pre-experiment) | Splits §9.3 into production deliverables (§9.3.A) vs. spike experiments (§9.3.B); 7 discrete gates with falsification clauses |
| v0.2.2 | (UNUSED) spike empirical fold-in (post-spike, all gates green) | Reserved for "all gates green" path; not reached — S1 falsified §8.2 |
| v0.3 | S1 spike-result fold-in; §8.2 falsified; §8.5 honestly re-narrated; S6 promoted; §9.3.A pacing split | Council ack-pass complete (dialectic NO-SHIP first pass with 2 BLOCKs about §8.5 framing — addressed via text-tightening, no architectural revisit); S2-S5 paused pending S6 |
| v0.4 | S6 spike-result fold-in (ambiguous); first verdict withdrawn after council; corrected verdict in KG; v0.4 lessons added to spike methodology | Council ack-pass complete; PR #278 merged 2026-05-01 |
| **RETIRED** | operator decision 2026-05-01 (post-v0.4) | **v0 closed; no v0.5.** Operator weighed S6 ambiguous-band (50-75% on 70% gate) as not-met; v0.3's "ambiguous defaults to falsified" rule honored; supervision-tree-recovery alone judged insufficient after §8.2 RSS prior collapsed; no new load-bearing leg invoked. Surviving work re-scoped to **Python discipline track** (separate). See §10 RETIREMENT entry below. |
| v1.0 | (UNREACHABLE — v0 retired) | v1.0 was reserved for post-cutover learnings; with v0 retired, this row is closed without ever being issued. Re-opens only if a future RFC ports the Pi-side broker to BEAM under a new operator-supplied premise. |

- **v0** (2026-04-30) — initial draft. Pre-council. Authored after archaeology session.
- **v0.1** (2026-04-30, same session) — council pass 1 amendments. Three NO-SHIPs returned. Eight BLOCKs addressed:
  1. §2 corollary corrected — broker calls UNITARES today; Pi corollary now places `unitares-bridge` Python sidecar explicitly (dialectic BLOCK).
  2. §3.4 LED honesty section added with v0.5 fold-in trigger (dialectic BLOCK).
  3. §6.1/§6.2 promotion gates rewritten with concrete diff thresholds, ASR baseline, regression triggers (dialectic + code-reviewer BLOCKs).
  4. §4.2 SHM lock parity contract spelled out; Elixir writer must use companion `.lock` file with fcntl LOCK_EX (code-reviewer BLOCK).
  5. §6.1 "zero-divergence" replaced with per-field-class diff thresholds (code-reviewer BLOCK).
  6. §4.2.1/§4.2.2 SHM envelope field enumeration; `governance` key gap closed via `unitares-bridge` parallel SHM channel; server fallback foreclosed from option (c) (code-reviewer BLOCK).
  7. §3.3 promotion bundles replace per-surface promotion (code-reviewer BLOCK).
  8. §9.1 lease plane runtime status stated explicitly; Phase A NOT gated on lease plane runtime (live-verifier BLOCK).

  Plus DRIFT corrections (voice dual-ownership, broker memory ~75-80 MB, `os._exit` already removed in `ed1b2f6`, BMP280 KG citation replaced with concrete anchors), §8.1/§8.5 steelmans, §7.4/§7.1/§7.6 council questions closed, §4.1 supervision tree split (I2C vs SPI vs GPIO), §3.3 hardware surface IDs reserved, provenance block added.

- **v0.2** (2026-04-30, same session) — ack-pass amendments. Two NO-SHIPs + one SHIP-with-caveats returned (live-verifier 12/12 v0.1 line citations VERIFIED, only §7.6 docs/schemas non-existence DRIFT). Seven new BLOCKs addressed via text-tightening (no architectural revisit):
  1. **§2.1 SQLite handle**: WAL mode + `busy_timeout=5000` discipline; `unitares-bridge` is sole writer for governance/EISV state; `anima` server is sole writer for self-model/preferences/learning; both may read freely (dialectic ack-pass BLOCK).
  2. **§2.1 identity-key freshness**: identity moved to bridge's SHM channel; broker no longer reads identity from disk (dialectic ack-pass BLOCK).
  3. **§2.1 UNITARES-call channel split**: bridge is sole UNITARES caller from Pi via `UnitaresBridge.check_in()`; broker emits operational telemetry only (`audit.tool_usage` with `tool_name=anima_broker_tick`, no governance payload) via separate channel (dialectic ack-pass BLOCK).
  4. **§4.2.3 social-boost SHM staleness**: `SOCIAL_BOOST_STALE_SECONDS = 10s` defined; lock-free pattern explicitly named as best-effort exception to the §4.2 lock-parity contract (dialectic ack-pass BLOCK).
  5. **§4.2.2 two-file cross-freshness**: `SHM_BROKER_STALE_SECONDS = 30s` + `SHM_BRIDGE_GOVERNANCE_STALE_SECONDS = 210s`; full server return-shape table for all 5 fresh/stale states (code-reviewer ack-pass BLOCK F1).
  6. **§7.6 + §9.3 JSON Schema gating**: schema file lives in `unitares/docs/schemas/`; `additionalProperties: true` at top level / `false` in sub-objects; corpus contract test as v0 deliverable; format-validator alignment between `jsonschema` and `ex_json_schema`; live-verifier DRIFT on directory-non-existence resolved by naming the canonical home (code-reviewer ack-pass BLOCK F2 + live-verifier DRIFT).
  7. **§4.1 SPI/GPIO supervisor strategy**: SPIHardwareSupervisor and GPIOSupervisor both moved to `rest_for_one` (was `one_for_one`) so bus-owner death cascades to children with stale handles; symmetric with I2C tree (code-reviewer ack-pass BLOCK F3).

  Plus CONCERN-level changes: §2.1 `:925-998` framing tightened (the actual `bridge.check_in()` is at `:982-989`; the range contains the call); §3.3 hw:/ IDs migrated to device-level granularity (`hw:/spi/spidev0.0`, full GPIO pin set D22/D23/D24/D27); §3.4 trigger reworded "I2C-conflict-class" → "shared-bus-conflict-class" (LEDs are SPI); §4.2 startup `.tmp` orphan cleanup added; §4.2.2 server-side change scope clarified as 5-site refactor (not "one-line"); §4.2.2 server fallback delivery framing reconciled (v0 deliverable + spike requirement + pre-Phase-A — read consistently as "must ship before Phase A starts; verified during spike"); §6.1 social-boost-window exclusion row added to diff thresholds; §6.1 `audit.tool_usage` field clarified (`error_type='shm_parse'`); §6.3 ordered systemd cutover transcript with PartOf= rewiring atomicity; §6.3 `unitares-bridge.service` unit-file sketch; §7.4/§7.6 specifics (strictness mode, validator equivalence, corpus contract test); §8.5 falsification clause for distribution leg; §9.3 Phase A pre-flight checklist rewritten as concrete deliverables (was spike list); §9.4.1 KG tag canonicalization (canonical `lumen-broker-port`, exact-match query); §9.5 Phase C cross-link; §10 explicit version ladder.

  v0.2 NITs not addressed in body but deferred to spike feedback: §2.1 diagram column conflation (LED + AutonomousVoice stacked in same column visually); diagram is correct in legend but column-stacking is cosmetic.

- **v0.2.1** (2026-05-01, post-merge of v0.2 PR #265) — **spike scope rescope (pre-experiment)**. v0.2's §9.3 mashed production deliverables together with spike experiments under one heading; this version splits them into §9.3.A (production deliverables — JSON Schema, server refactor, bridge service, audit instrumentation) and §9.3.B (the actual spike — 7 discrete experiments with go/no-go gates).

  The spike grew from "BMP280 GenServer reads + telemetry, ~3 days" to a 7-experiment ladder (~5 days, ~5-7 calendar days with parallel work):
  - S1: cold-start sanity (falsifies §8.2 RSS claim if > 60 MB)
  - S2: BMP280 GenServer (parity with `smbus2`)
  - S3: SHM lock parity (falsifies §4.2 lock-parity contract if torn writes)
  - S4: supervisor cascade (falsifies §4.1 v0.2 strategy decision if behavior differs)
  - S5: bridge stub + typed-absence (falsifies §4.2.2 two-file freshness table)
  - S6: distribution falsification — does PyOxidizer/Nuitka deliver ≥70% of BEAM's distribution win at ≤20% the cost? If yes, §8.5 conjunction collapses, project rethinks
  - S7: divergence comparator dry-run (24h sample with §6.1 thresholds active)

  Each experiment names a falsifier explicitly. The spike outcome decision tree (in §9.3.B) maps each gate failure to a specific RFC version bump (v0.2.2 textual fold-in if all green; v0.3 architectural revisit if S4 or S5 fails; operator escalation if S6 falsifies).

  **No architectural change.** This version only restructures §9.3 and adds the v0.2.1, v0.2.2 entries to the version ladder. All other sections unchanged from v0.2.

- **v0.3** (2026-05-01, post-merge of v0.2.1 PR #272) — **S1 spike-result fold-in; §8.2 prior falsified; §8.5 honest re-narration of the unchanged two-leg conjunction; S2-S5 paused; §9.3.A pacing split.**

  S1 ran on Lumen 2026-05-01. Empirical findings (KG `2026-05-01T09:29:02.864022+00:00` severity high tag `lumen-broker-port`):

  - Tarball: 5.72 MB (gate <30 MB, **PASS** ~5x margin).
  - Cold-start: 1.42 / 1.45 / 1.61 s across 3 runs (gate <5 s, **PASS** ~3x margin).
  - Idle RSS: 123.7 MB stable @ 30/60/120s on Pi 4B / Trixie / aarch64 / OTP 27 / Elixir 1.18 (gate ≤40 MB go, >60 MB falsifies; **FALSIFIES** §8.2).

  Comparison set (apples-to-apples /proc/<pid>/status VmRSS): bare ERTS 52 MB, default Elixir release 124 MB, Python broker today 77 MB. Strip-beams + `+S 1:1 +MMscs 0` did NOT move the needle (124.9 MB vs 123.7 MB), confirming the +71 MB gap from bare ERTS is not per-scheduler arena cost — it is OTP boot script preload + Elixir runtime libs + initial allocator carveouts.

  **Operator directive (2026-05-01):**
  1. Accept S1 as a real falsification. Do NOT re-anchor §8.2 threshold after the fact.
  2. Halt S2-S5 under the v0.2.1 decision tree; resume only after v0.3 council ack-pass + S6 result.
  3. Do NOT jump to Nerves. Nerves is a separate embedded-port RFC unless operator explicitly authorizes "Lumen becomes an appliance OS" — see §7.1 v0.3.
  4. Run S6 (Python distribution falsifier) next. S6 directly answers whether the appliance-distribution leg of §8.5 still stands.
  5. Honest framing of v0's case: supervision-tree-recovery alone is not enough — that was already true in v0.2 — and the §8.2 prior that made the appliance-distribution leg cheap to argue for is now gone, so the same two-leg conjunction is harder to defend. The leg menu is operator's call, not pre-supplied by v0.3.

  **Council ack-pass on v0.3 amendments (parallel agents, 2026-05-01, scoped to v0.3 changes only):**
  - dialectic-knowledge-architect: NO-SHIP first pass — 2 BLOCKs (§8.5 conjunction recompute was revisionist by inventing a "third memory leg" that wasn't in v0.2's two-leg framing; "after the memory leg collapsed" implied a state change that didn't happen) + 2 CONCERNs + 3 DRIFTs. Addressed in v0.3 body before merge: §8.5 rewritten to narrowly state "implicit §8.2 prior falsified" rather than "memory leg collapsed"; supervision-tree-recovery's standing as load-bearing-only-in-conjunction is named as unchanged from v0.2; §8.1 caveated re S6 gating; §9.3.A pacing split added; §7.1 label tightened from "NAMED-OPTION-PENDING" to "CLOSED in v0; SEPARATE-RFC-CONDITIONAL"; §9.3.B decision tree gained explicit S2-S5 + S7 failure mapping inside the post-S6-holds branch.
  - feature-dev:code-reviewer: SHIP with 1 CONCERN (S2-S5 fail paths missing from new tree — addressed) + 2 NITs (§8.2 `:erlang.memory()` sentence ambiguity — tightened; §9.2 §7.6 row pre-existing CLOSED-label inconsistency — pre-existing from v0.2, deferred). Numeric internal consistency verified across all five `/proc/<pid>/status` rows.
  - live-verifier: SHIP-with-caveats; 6 VERIFIED + 2 minor DRIFTs (Elixir version-string suffix `dfsg-1`/`27.3.4` not visible in runtime banner — resolved in v0.3 by separating runtime-banner facts from `dpkg -l` package versions; tarball byte count 6,000,680 in original measurement vs 6,000,657 on disk after the strip_beams TEST_B rebuild — resolved by listing both reproducible byte counts in the tarball gate row).

  **Sections amended in v0.3:**
  - §8.1 — added one parenthetical caveat that the appliance-distribution leg is now S6-gated; otherwise unchanged.
  - §8.2 — replaced unverified "25-40 MB" estimate with measured facts (52 / 124 / 77 MB comparison set); cited S1 KG anchor; separated runtime-banner Elixir/erts facts from `dpkg -l` package versions; tightened the `:erlang.memory().total = 52.5 MB` sentence to be unambiguous.
  - §8.5 — explicit honest narration of the v0.2 two-leg conjunction as unchanged in form; S1 falsifies an *implicit positive prior* in §8.2, not an enumerated leg; the appliance-distribution leg is weaker (now gated on S6) but not falsified; supervision-tree-recovery's standing is unchanged from v0.2; "after the memory leg collapsed" causal framing removed; pre-supplied leg menu removed (operator-supplied additional leg framing kept).
  - §7.1 — relabeled "CLOSED in v0; SEPARATE-RFC-CONDITIONAL on S6 + appliance-OS framing" (replaces v0.3 first-pass "NAMED-OPTION-PENDING" which the dialectic ack-pass flagged as misleading); explicit statement that v0.3 does NOT re-open Nerves within this RFC's decision space.
  - §9.2 council pass items — §7.1 row updated to match the new §7.1 label.
  - §9.3.A — pacing split: server-fallback typed-absence path + audit.tool_usage write path continue (independent value); JSON Schema + validator corpus test + unitares-bridge.service systemd unit pause (Phase-A-only value, gated on S6 holding).
  - §9.3.B — gate table reshaped as a status table (S1 DONE, S6 NEXT, S2-S5 + S7 PAUSED); spike methodology note added (`pgrep -f beam.smp` false-positive lesson with three actionable alternatives); v0.2.1 decision tree explicitly superseded; v0.3 decision tree adds an explicit S2-S5 + S7 failure mapping inside the post-S6-holds branch (S2/S3 fail → retire; S4/S5 fail → v0.5 amendment; S7 fail → threshold-tuning v0.5).
  - Frontmatter status, top-of-doc banner, §10 version ladder updated to reflect v0.3 as CURRENT and v0.2.2 as UNUSED (the "all gates green" path was not reached).

  **Sections unchanged in v0.3:** §1, §2, §3, §4, §5, §6, §7.2-§7.7 (other than §7.1), §8.3, §8.4, §9.1, §9.4, §9.4.1, §9.5. The architecture, rollout plan, schema contracts, lease-plane status, supervision strategy, hardware ownership lines, and crash-recovery edge cases do not change in v0.3 — what changes is the case for proceeding at all, the pacing of §9.3.A deliverables, and the order of remaining spike experiments.

  **No new architectural decisions.** v0.3 is a falsification fold-in plus a re-prioritized spike order plus a §9.3.A pacing split. The v0.4 amendment after S6 is where the next architectural decision lives (port vs retire vs reframe). Per-experiment failures inside the post-S6-holds branch route to v0.5 amendments or retire decisions, not to in-band substrate switches.

  v0.3 is **post-ack-pass**. Implementation gate stays closed for the paused §9.3.A items until S6 holds; the two §9.3.A "continue" items may proceed independently.

- **v0.4** (2026-05-01, post-merge of v0.3 PR #275) — **S6 spike-result fold-in (ambiguous); first verdict withdrawn after council; corrected verdict in KG; v0.4 lessons added to spike methodology.**

  S6 ran on Lumen 2026-05-01 after v0.3 council ack-pass cleared. First verdict was filed at KG `2026-05-01T11:02:42` claiming §8.5 distribution leg was FALSIFIED. A 3-agent council adversarial review (parallel agents, scoped to S6 verdict only) returned:

  - **dialectic-knowledge-architect**: NO-SHIP-with-correctable-fix. 4 BLOCKs:
    - B1: re-committed §8.5 v0.3 leg-menu pre-supply violation (verdict enumerated Branches 1+2; v0.3 §8.5 line 569 explicitly says "leg menu is operator-supplied, not pre-supplied")
    - B2: status-quo bias counting Python's existing systemd wiring as a Python lead (would also exist post-port)
    - B3: shiv pinning deploy to apt-python is a real ecosystem concession, not a "close match"
    - B4: Nuitka killed at 43:21 not measured to fail; Pi-build time is wrong axis (real release pipelines build on dev machine)
  - **feature-dev:code-reviewer**: NO-SHIP. 3 BLOCKs:
    - B1 (95% conf): apples-to-oranges — `mix new --sup` skeleton (5.72 MiB) vs full Python broker shiv (43.35 MiB). Realistic Elixir broker tarball with hardware deps would be 15-30 MiB+; size verdict could reverse.
    - B2 (90% conf): only timed `import` of shiv pyz, never executed `anima-creature` end-to-end. Native C ext chain could fail at `dlopen` time.
    - B3 (85% conf): unit convention inconsistency (Elixir 5.72 stated as MiB-as-MB, shiv 36.9 as SI-MB).
  - **live-verifier**: SHIP-with-caveats; 9 VERIFIED, 2 DRIFTs, 1 REFUTED:
    - REFUTED: shiv pyz CANNOT execute. `python3 anima-creature.pyz` fails with `FileNotFoundError: site-packages/bin/anima-creature` — build defect (`--console-script` flag mis-spec). The "single-file artifact" claim rested on an import test against an artifact that didn't actually run end-to-end.
    - DRIFT: PR #275 was already MERGED before verdict was filed.

  v0.4 actions taken to address the council:

  1. **First S6 verdict WITHDRAWN**: KG `2026-05-01T11:02:42` status updated to `superseded`; supersession note filed at `2026-05-01T11:22:20`.
  2. **shiv build fixed**: changed from `--console-script anima-creature` (which expected a `bin/X` script in the venv) to `--entry-point anima_mcp.stable_creature:main` (proper module-entry spec). Resulting pyz is 43.346 MiB / 45,451,738 B (vs broken 35.166 MiB / 36,875,827 B from the prior `--console-script` build) and verified runnable end-to-end via timeout-and-shutdown trace (`[StableCreature] Creature 'Lumen' is alive` → `Entering main loop` → `Shutdown signal received` → exit=124 from SIGTERM).
  3. **Realistic Elixir release built**: `mix new --sup` + circuits_i2c + circuits_gpio + circuits_spi + bandit + plug + jason; 47s build time on Pi 4; 7.262 MiB tarball / 19 MiB unpacked / 143.4 MiB idle VmRSS. Apples-to-apples comparison restored.
  4. **Corrected S6 verdict** filed at KG `2026-05-01T11:32:43` severity `medium`. Aggregate distribution-win 50-75% / cost ratio 7.5-25% — qualitatively ambiguous (axis-weighting variance) on the 70%/20% gate. Verdict declines to pre-decide on operator's behalf.
  5. **Nuitka downgrade**: from "build did not complete in 45min on Pi 4 (a cost data point)" to "NOT MEASURED — killed at 43:21 mid-gcc; Pi-side build time is wrong axis". Acknowledged that completing Nuitka on a dev machine would resolve the ambiguity.

  **Sections amended in v0.4:**
  - §8.5 — folded corrected S6 measurements (5.97x size penalty / 11.7x footprint penalty / +70.4 MiB Python memory advantage); aggregate 50-75% on 70% gate; three resolution paths for operator (complete Nuitka / weight axes / invoke additional leg); §1.1 items 2 + 3 (PR #11 hand-rolled bus retry, PR #14 D22/D24 refresh hack) named explicitly as bugs that "Python + discipline" leaves as-is.
  - §9.3.B — gate table reshaped: S1 DONE, S6 DONE-AMBIGUOUS, S2-S5 + S7 stay PAUSED. Decision tree annotated with v0.4 status (ambiguous = current branch; falsifies/holds = not current). v0.4 lessons added to spike methodology (apples-to-apples / runnability / killed-≠-failed / honor leg-menu-is-operator-supplied).
  - §10 — version ladder: v0.3 marked complete; v0.4 added as CURRENT; v1.0 gated on v0.5 keeping the port alive (was v0.4).
  - Frontmatter status, top-of-doc banner: reflect v0.4 / S6-ambiguous / S2-S5 paused.

  **Sections unchanged in v0.4:** §1, §2, §3, §4, §5, §6, §7, §8.1, §8.2, §8.3, §8.4, §9.1, §9.2, §9.3.A, §9.4, §9.4.1, §9.5. Architecture, rollout plan, schema contracts, lease-plane status, supervision strategy, hardware ownership lines, crash-recovery edge cases unchanged.

  **No new architectural decisions.** v0.4 is a measurement fold-in plus a status-table update plus methodology lessons. The next architectural decision lives in either (a) a v0.5 amendment if v0 continues, or (b) explicit retire-without-amendment if operator chooses retire-to-Python-discipline or appliance-OS reframing — the v0.3 tree permits closure without amendment, and v0.4 does not pre-suppose v0 continues.

  **Council ack-pass on v0.4 amendments (parallel agents, 2026-05-01, scoped to v0.4 changes only):**
  - dialectic-knowledge-architect: NO-SHIP-with-correctable-fix — 1 BLOCK (§9.3.B S2 row enumerated a/b/c as a closed list, re-committing the very leg-menu rule violation that §8.5 v0.4 lessons just installed) + 2 CONCERNs (default-posture divergence "treat-as-falsified → S2-S5 PAUSED" softens v0.3's literal retire-default; severity-downgrade rationale + Pi-CI runner caveat slipped into §10 narrative without §8.5/§9.3.B body support) + 1 NIT (instrument-noise vs axis-weighting-variance) + 1 DRIFT (pre-deciding v0.5 will exist). Addressed in v0.4 body before commit: S2 row rewritten to drop a/b/c enumeration and cross-reference §8.5 v0.4; explicit divergence acknowledgment paragraph added to §8.5 v0.4; severity rationale struck from §10; lsof CI runner caveat folded into §9.3.B S6 cost row; "instrument noise" replaced with "axis-weighting variance" everywhere; version ladder + narrative reframed to allow retire-without-amendment.
  - feature-dev:code-reviewer: NO-SHIP — 1 BLOCK (`45.35 MiB` shiv pyz size in §9.3.B and §10 was the SI-MB value mislabeled as MiB; correct MiB-from-byte-count is `43.346 MiB`; this re-introduced the EXACT unit-convention error that was B3 of the withdrawn first S6 verdict) + 2 CONCERNs (S2 condition (a) "fails-cleanly" register-shift; §8.5/§9.3.B same-three-paths divergence in wording) + 1 NIT (frontmatter status field too narrative). Addressed: all `45.35 MiB` → `43.346 MiB`; downstream `222 MiB total` → `~220.3 MiB total` (43.346 + 177); S2 row rewritten (also resolves "fails-cleanly" register-shift); frontmatter status trimmed to one-liner.
  - live-verifier: SHIP — 8 VERIFIED, 2 minor DRIFTs (broker uptime advanced 3d→12d since measurement; broker VmRSS drifted 73.0 → 75.0 MiB over 12 days — both staleness-of-snapshot, not fabrications). shiv pyz runnability VERIFIED end-to-end (`[StableCreature] Creature 'Lumen' is alive` → `Entering main loop` → `Shutdown signal received` → exit=124 from SIGTERM).

  All v0.4 BLOCKs textual; no architectural revisit required.

  Implementation gate stays closed for paused §9.3.A items until operator decides on S6.

  KG anchors: corrected S6 verdict `2026-05-01T11:32:43`; withdrawn S6 verdict `2026-05-01T11:02:42` (status=superseded); withdrawal note `2026-05-01T11:22:20`; S1 verdict `2026-05-01T09:29:02`; v0.3 RFC entry `2026-05-01T09:59:41`.

- **RETIRED** (2026-05-01, post-merge of v0.4 PR #278) — **operator decision: v0 closed without v0.5; surviving work re-scoped to Python discipline track.**

  **Operator directive (verbatim summary, 2026-05-01):**
  1. Do not complete Nuitka now. The shiv result + Elixir RSS falsification is enough for this RFC decision. Nuitka can be a later packaging spike for Python hardening, not a blocker for retiring the BEAM broker port.
  2. Do not invoke a new load-bearing leg. No substrate-uniformity or supervision-tree recovery added as replacement justification. After §8.2 collapsed, the remaining case is not strong enough for a full Pi-side BEAM port.
  3. Treat the 70% bar as not met decisively enough to continue. S6 landed in the ambiguous band, and v0.3's rule was "ambiguous defaults to falsified." Keep that.
  4. Keep the useful Python-side hardening. Re-scope the surviving work into a new Python discipline track:
     - typed-absence server fallback (was §9.3.A "continue" item; ships independently)
     - audit.tool_usage write path (was §9.3.A "continue" item; ships independently)
     - single-writer hardware ownership checks (lsof CI)
     - watchdog/restart/runbook packaging improvements
  5. No v0.5 unless there is a real new premise. A v0.5 should only happen if we later choose "Lumen as appliance OS" or get surprising evidence from Python packaging/hardware discipline work.

  **What this RFC's archive value is:** the design content of §1-§7 (problem framing, supervision tree, hardware ownership lines, JSON schema contract, lease-plane cross-link, OPEN questions) and §8 (concerns/counter-arguments) preserves the analysis of *why* the Pi-side BEAM port did not survive its first measurement spike. Future agents proposing a port should read v0-v0.4 first to understand which arguments did not survive S1 (§8.2 RSS estimate) and S6 (§8.5 distribution-leg ambiguity), and to avoid recomputing those falsifications.

  **What stays committed in code (independent value):** the two §9.3.A "continue" items will ship under the Python discipline track:
  - Server fallback typed-absence path (multi-site refactor per §4.2.2): eliminates `SERVER_GOVERNANCE_FALLBACK_SECONDS` direct-UNITARES code path; prevents BMP280-wedge-class fallback recurrences in the existing Python broker.
  - `audit.tool_usage` write path: broker telemetry + `error_type='shm_parse'` regression trigger; reusable for any broker writing the SHM envelope.

  **What stays as-is (acknowledged trade-offs):** §1.1 items 2 + 3 — PR #11's hand-rolled `recreate I2C bus handle when multiple sensors fail` and PR #14's periodic 30s D22/D24 OUTPUT-HIGH refresh hack — remain in production. Single-writer-to-hardware enforcement remains code-review-only (PR #45 closed BMP280 wedge; lsof CI to be added under Python discipline track). These are the bugs the OTP-shaped-win argument was meant to fix structurally; "Python + discipline" leaves them as-is. Trade-off named, not papered over.

  **Spike artifacts** at `~/spike-s1/spike/` (Elixir hello-world, S1) and `~/spike-s6/{shiv,nuitka,elixir/realistic}` (S6 measurements) on Lumen are preserved for archival reference and future reproduction.

  **No further amendments planned.** This entry is the canonical retirement record. KG entries: retirement `2026-05-01T12:23:47.750670+00:00` severity=high tag=`v0-retired,lumen-broker-port`; Python discipline track `2026-05-01T12:24:16.916553+00:00` severity=medium tag=`python-discipline-track`.

  Re-opens only if (a) operator authorizes "Lumen becomes an appliance OS" framing (separate Nerves-port RFC, NOT a v0.5 of this one), or (b) surprising evidence emerges from the Python discipline track that re-frames the §8.5 conjunction.

