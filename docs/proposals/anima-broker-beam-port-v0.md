---
status: DRAFT-v0, pre-council
authored: 2026-04-30
author_session: agent-c9e03e26-33c (claude_code-claude_c9e03e26)
related:
  - docs/proposals/surface-lease-plane-v0.md (the first BEAM wedge, Mac-side; this is the second wedge, Pi-side; same operator decision in KG `2026-04-30T19:30:54.644112+00:00` framed leases as first wedge and "full BEAM nervous system" as destination)
  - docs/ontology/beam-coordination-kernel.md (parallel ontology-track framing — UNITARES R7 row in `docs/ontology/plan.md`)
  - PR #45 anima-mcp `fix(sensors): server reads SHM, never opens /dev/i2c-1` (the BMP280 wedge — single-writer-to-hardware violation)
  - PR #14 anima-mcp `Periodically refresh D22/D24 to prevent TFT blackout` (D22/D24 GPIO shared-pin race between TFT and joystick)
  - PR #11 anima-mcp `fix(sensors): recreate I2C bus handle when multiple sensors fail` (bus-wedge recovery)
  - PR #8 anima-mcp `fix(server): swallow MCP SDK ClosedResourceError on client disconnect` (anyio cousin)
  - commit `c83748c test: add regression coverage for shutdown ownership + warmup race` (anima-mcp)
  - KG `2026-04-28T*` BMP280 wedge incident + `feedback_trust-operator-pattern-over-data-anchor.md` (operator-pattern-over-data-anchor lesson; lsof showed architecture violation while I called hardware)
out_of_scope_explicit: |
  Hard line — load-bearing substrate boundaries (inherited from `surface-lease-plane-v0.md` §invariant):
  - Distributed Erlang clustering between Pi BEAM node and Mac BEAM node (each node is single-node; cross-host coordination uses Postgres or HTTP, never Erlang clustering)
  - Identity issuance, EISV math, KG writes, calibration — these stay in Python on the Mac

  Deferred to subsequent RFCs (each merits its own scope):
  - LED hardware ownership cleanup (today the broker owns sensors+face, the server owns LEDs — split-brain; cleanup is its own decision)
  - Phoenix LiveView replacement of TFT display rendering pipeline (Pillow-based today; LiveView+canvas is a Mac-side analogy, here it would be HTML-on-Pi)
  - Voice / TTS subsystem port
  - Cross-language type generation (Pydantic↔Ecto schemas)
unblocks: |
  - Single-writer-to-hardware enforcement structurally (today it's convention, repeatedly violated)
  - Shared-pin GPIO races (D22/D24 today coordinated by periodic-refresh hack)
  - Bus-wedge recovery (today: recreate-handle-on-failure heuristic; with OTP: supervisor restarts the GenServer that owns the bus, lease released on death)
  - Distribution: Lumen as appliance ships a single Elixir release tarball, not Python+venv+system-deps+service-files
---

# Proposal: Anima Broker BEAM Port v0 (Pi-side coordination kernel)

> **Status: DRAFT-v0, pre-council.** This document is a follow-on to `surface-lease-plane-v0.md`. The lease plane is the **Mac-side** first wedge for BEAM/OTP. This RFC is the **Pi-side** second wedge: porting the `anima-creature` broker to a single-node Elixir application that owns Lumen's hardware lifecycle. Both nodes are single-node by design (no Distributed Erlang between them); they coordinate via the same patterns the Python fleet uses today (HTTP, Postgres heartbeat-TTL).

## 1. Problem

The Pi-side broker (`anima-creature`, `src/anima_mcp/stable_creature.py`, 1299 LOC Python) sits in a class of bugs that OTP was built to make boring. The git trail shows a steady diet:

1. **Single-writer-to-hardware violations.** PR #45 (`fix(sensors): server reads SHM, never opens /dev/i2c-1`) closed the BMP280 wedge: the Python anima-mcp server had been opening I2C directly while the broker also held it. Detection took days because nothing structurally prevented it; lsof eventually showed the violation. Operator-pattern-over-data-anchor memory `feedback_trust-operator-pattern-over-data-anchor.md` is this incident's lesson.

2. **Bus-wedge recovery as application logic.** PR #11 hand-rolled "recreate I2C bus handle when multiple sensors fail." This is a manual reinvention of `Supervisor.restart_strategy: :rest_for_one`.

3. **Shared-pin GPIO races.** PR #14 (`Periodically refresh D22/D24 to prevent TFT blackout`) — TFT display and joystick share pins; the periodic refresh is a brittle workaround for the absence of a single owner of pin state.

4. **Shutdown / warmup ordering races.** Regression test in `c83748c` exists because shutdown ownership and warmup race against each other; tested but not structurally prevented.

5. **anyio-asyncio cousin bugs.** PR #8 (`swallow MCP SDK ClosedResourceError on client disconnect`) is a smaller version of the deadlock class that motivated `surface-lease-plane-v0.md` Mac-side.

6. **Distribution friction.** Lumen-as-appliance is in the project goal set (per session 2026-04-30 conversation). Today's deploy is "Pi reflash → restore_lumen.sh → venv + system deps + service files." This is shippable to operators (Kenny), not to non-developer end-users.

Bucketed against the same diagnostic the lease plane uses (concurrent mutable state | async runtime coupling | fanout/backpressure | authority/stale truth), the Pi-side incident class lands ~5 of 6 in buckets 1-2.

## 2. Decision

Port `anima-creature` to a **single-node Elixir/OTP application** running on the Pi. Hardware ownership lives in OTP processes under a supervision tree; the supervisor IS the recovery story; lease release on death is automatic.

Inherit the lease-plane RFC's invariant verbatim:

> BEAM owns live coordination.
> Python owns governance truth.
> Postgres owns durable truth.
> No BEAM component may silently become source of truth for identity, EISV, KG, or calibration.

**Pi-specific corollary**: BEAM owns hardware lifecycle. Python anima-mcp server stays in Python; it reads through the BEAM broker the same way it reads through the Python broker today.

Ship in **shadow mode first** (Elixir broker reads sensors in parallel, writes its own SHM channel; Python broker keeps running and remains source of truth), promote to **swap mode** (Elixir writes the SHM channel the server reads from; Python broker retired), and only then **cutover** (Python broker process removed from systemd).

## 3. Scope (in / out)

### 3.1 In scope (v0)

- A new Elixir application running on the Pi, separate process from the Python anima-mcp server.
- OTP supervision tree owning: I2C bus, BMP280 sensor, accelerometer/gyro sensor, TFT display, joystick GPIO, voice/TTS subsystem (read-only — see §3.2), face-state derivation, metacognitive reflection serialization.
- SHM-compatible JSON write to `/dev/shm/anima_state.json` matching the current envelope shape (see `src/anima_mcp/shared_memory.py:write`) — same wire, different writer.
- Health endpoint over HTTP (parity with current Python broker).
- OTP application-controlled clean shutdown ordering (no `os._exit` workarounds — see anima-mcp `15eda1f Fix deploy restart`).
- Telemetry emission to UNITARES `audit.tool_usage` (or §6.1-equivalent) via HTTP, same channel the lease plane uses.
- Nerves vs. vanilla Elixir choice (§7.1) — a real decision, not assumed.

### 3.2 Out of scope (v0)

Listed in the frontmatter `out_of_scope_explicit` field. The largest shape:

- LED hardware ownership stays where it is today (server owns LEDs, broker doesn't import `LEDDisplay`). This is a **conscious carve-out**: the LED split-brain is a separate cleanup with its own design choices, and bundling it into the BEAM port doubles the surface and makes the rollback story worse.
- Voice/TTS subsystem is read-only in v0 — Elixir broker reads voice config and serializes it for SHM, but does not own the speech synthesis pipeline.

### 3.3 Surfaces enumerated for v0 (shadow mode)

Initial shadow registrations. Python broker remains source of truth in v0; the Elixir broker writes a parallel SHM channel for diff comparison.

| Surface | Owner (today) | Owner (v0 Elixir) | Notes |
|---|---|---|---|
| `/dev/i2c-1` (bus) | Python broker | Elixir `I2CBus` GenServer (shadow) | Shadow read-through; Python still primary writer |
| BMP280 sensor | Python broker | Elixir `BMP280` GenServer (shadow) | Read at same cadence; diff into telemetry |
| Accel/gyro | Python broker | Elixir `IMU` GenServer (shadow) | Same |
| TFT display (SPI) | Python broker | Elixir `TFTDisplay` GenServer (shadow) | Renders to off-screen buffer; not actually drawn in shadow mode |
| Joystick GPIO | Python broker | Elixir `Joystick` GenServer (shadow) | Read-only; no claim on D22/D24 |
| Face-state derivation | Python broker | Elixir `FaceState` (pure module) | Both compute; diff |
| Metacognitive reflection | Python broker | Elixir `Reflection` GenServer | Both serialize; diff |
| LED display | Python server | Python server (unchanged) | OUT OF SCOPE v0 |
| Voice / TTS write | Python broker | Python broker (unchanged) | OUT OF SCOPE v0 |

The shadow→swap promotion gate is per-surface, not all-or-nothing. Sensors flip first (low blast radius if Elixir is wrong, easy to A/B compare). Display flips last (visible regression if wrong).

## 4. Architecture (sketch)

### 4.1 Supervision tree

```
AnimaBroker.Application
└── AnimaBroker.Supervisor (one_for_one)
    ├── AnimaBroker.HardwareSupervisor (rest_for_one)
    │   ├── AnimaBroker.I2CBus           (owns /dev/i2c-1; killing this kills sensor children)
    │   ├── AnimaBroker.BMP280
    │   ├── AnimaBroker.IMU
    │   ├── AnimaBroker.GPIOBus          (owns BCM pin claims)
    │   ├── AnimaBroker.TFTDisplay       (uses GPIOBus for D22/D24)
    │   └── AnimaBroker.Joystick         (uses GPIOBus for joystick pins)
    ├── AnimaBroker.SHMWriter            (writes /dev/shm/anima_state.json envelope)
    ├── AnimaBroker.HealthEndpoint       (Bandit/Plug HTTP)
    └── AnimaBroker.Telemetry            (HTTP forwarder to UNITARES audit channel)
```

`rest_for_one` on the hardware tree is the load-bearing strategy: if the I2C bus wedges and the GenServer owning `/dev/i2c-1` dies, every sensor child below it restarts as well, and they all reacquire from a fresh bus handle. This is what PR #11's manual recreate-handle code is hand-rolling.

### 4.2 Wire to Python anima-mcp server

v0 keeps the **same SHM wire** (`/dev/shm/anima_state.json` JSON envelope, fcntl LOCK_EX, atomic temp+rename). The reason: the Python server is large, in production, and out of scope for this RFC. Changing the wire and the writer in the same change doubles the rollout risk.

Future RFC: replace SHM with a typed protocol (gen_statem-fronted GenServer over HTTP, or Elixir Port to a Python child, or Phoenix Channel — explicitly deferred).

### 4.3 Hardware ownership lines (the key invariant)

Every hardware resource is owned by exactly one OTP process. "Ownership" means: holds the file descriptor / GPIO claim / SPI handle. To read or write the resource, you `GenServer.call` the owner. Direct hardware access from outside the supervision tree is structurally prevented by the OS (the FD lives in the BEAM VM's process), and is also enforced socially (no `:os.cmd` calls into hardware; no escape hatches).

This is the structural fix the BMP280 wedge needed: in Elixir, the Python server *cannot* open `/dev/i2c-1` because the BEAM process holds the FD. The architecture violation that took days to detect is impossible by construction.

## 5. API surface (v0)

The broker exposes:

- HTTP `/health` — same shape as today (returns `{"status": "ok", "subsystems": {...}}`).
- HTTP `/sensors` — current sensor snapshot (mirror of SHM envelope; for diagnostic curl).
- SHM write to `/dev/shm/anima_state.json` — same wire as today.
- Telemetry POSTs to UNITARES audit channel — same shape as Mac-side lease plane.

No new public API in v0. The point of v0 is to swap the runtime, not the contract.

## 6. Rollout (shadow → swap → cutover)

### 6.1 Phase A — Shadow (week 1-2)

- Elixir app runs alongside Python broker on the Pi.
- Reads sensors at the same cadence; writes to a parallel SHM channel `/dev/shm/anima_state_elixir.json`.
- Telemetry emits diff between Python and Elixir envelopes; UNITARES audit ingests.
- Promotion criterion: 7 days of zero-divergence diffs (or root-caused divergences), no ASR drop visible in dashboard fleet metrics.

### 6.2 Phase B — Swap (week 3-4)

- Per surface (sensors first, display last), Elixir writes the canonical SHM file the server reads from. Python broker continues running but its SHM writes are gated to a backup channel.
- Promotion criterion (per surface): 3 days post-swap with no regression.

### 6.3 Phase C — Cutover (week 5)

- Python broker removed from systemd. anima-mcp server runs unchanged.
- `stable_creature.py` archived (not deleted) for one release cycle in case rollback is needed.

## 7. Open RFC questions (council MUST answer)

### 7.1 Nerves vs. vanilla Elixir on Raspbian

Nerves (purpose-built Elixir embedded distro) gives a smaller footprint and atomic A/B firmware updates. Vanilla Elixir on Raspbian is operationally simpler for a one-Pi fleet and keeps the existing Pi management story (Tailscale, backup scripts, reflash playbook). v0 leans toward **vanilla Elixir on existing Raspbian** because the operational delta of Nerves is its own scope. Council to confirm or reject.

### 7.2 SHM JSON envelope: keep, or migrate to a typed format

The current envelope is a JSON dict with no schema. Server tolerates missing keys via `.get()`. Elixir will emit the same shape, but the council should decide whether v0 also introduces an Ecto schema with strict typing (and the migration tax that implies for the Python server's `.get()` tolerance).

### 7.3 LED hardware: stay split, or fold into v0

Today the server owns LEDs and the broker owns sensors+face+display. Folding LEDs into the broker port is "the right thing" architecturally but doubles v0 surface. Recommendation: stay split in v0; LED ownership is its own RFC.

### 7.4 What if the Elixir broker is down?

Mac-side lease plane RFC §7.7 has the analog answer: typed-absence return shapes, Python clients fail-open in advisory mode. Pi-side analog: if the Elixir broker is down, what does anima-mcp server do? Options: (a) read stale SHM and serve stale state; (b) fail health check to UNITARES; (c) fall back to direct hardware reads (re-introducing the wedge class). Council to decide.

### 7.5 Hot-code reload

Lease plane RFC §3.5 narrows hot-reload claims to stateless modules. Hardware-owning GenServers are *deeply* stateful (FD, calibration, peripheral handshake state). Hot-reload is probably explicitly NOT a v0 promise. Council to confirm "hot-reload is out of scope for v0; supervisor restart is the upgrade story."

### 7.6 Cross-language schema source-of-truth

Python anima-mcp server has Pydantic models for the SHM envelope shape. Elixir broker will need an equivalent. Generated bindings (e.g., JSON Schema → both sides), or hand-mirrored with a contract test? §3.5 in this RFC's "Architecture / wire" section deliberately punts; council should decide whether v0 ships a contract test.

### 7.7 Failure-mode for shared-pin races

D22/D24 (TFT + joystick) race fix today is periodic refresh. With Elixir's GPIOBus owning all pin claims, the race becomes "two GenServers both try to acquire the same pin" — which is a simple `GenServer.call` ordering problem, not a refresh-loop hack. Confirm: yes, periodic refresh is removable in Phase B. Or: surface a real reason it can't be.

## 8. Concerns / counter-arguments / minority views

### 8.1 "Python's been working. Why migrate?"

Python is working in the sense that the broker process stays up. It is not working in the sense that PRs #45, #14, #11, #8, and the shutdown-ownership regression test exist as fixes for a class of bug the language can't structurally prevent. OTP makes this class boring.

### 8.2 "BEAM is heavy on a Pi 4."

Vanilla Elixir resident memory ~25-40 MB. Pi 4 has 4 GB or 8 GB. The broker today peaks ~80 MB Python. This concern was the load-bearing reason I (the author) originally recommended Go in conversation; the operator-decision-was-already-made archaeology corrected that. BEAM footprint is real but small.

### 8.3 "You'd be debugging hardware drivers in BEAM. That's an unfamiliar stack."

True, and not nothing. The mitigation: `circuits_i2c`, `circuits_gpio`, `circuits_spi` are well-maintained Hex packages used by Nerves; they wrap the same Linux kernel devices Python's `smbus2` / `RPi.GPIO` / `spidev` use. Driver semantics are the same; the runtime around them is what changes. This is a real cost — flagged, not minimized.

### 8.4 "Why not Go?"

Same answer as the lease-plane RFC §8.3: Go gives cheap concurrency but no supervision primitive — you'd rebuild OTP yourself with channels + recover. The unifying argument also applies: Mac-side BEAM + Pi-side Go gives you two supervision models to learn instead of one. The 2026-04-30 KG operator decision (`beam-spike-greenlit, goal-not-wedge`) chose BEAM for exactly this reason.

### 8.5 "This is just substrate migration tax dressed as architecture."

Honest answer: yes, partially. The architectural win is structural single-writer-to-hardware enforcement. The migration win is appliance-shaped distribution (Elixir release tarball vs. Python+deps). Both are real; neither alone is sufficient. The conjunction is.

## 9. Pre-implementation checklist

- [ ] §7.1 Nerves vs. vanilla — operator decision
- [ ] §7.2 SHM envelope schema — council
- [ ] §7.3 LED scope — council confirms stay-split
- [ ] §7.4 down-mode behavior — council decides
- [ ] §7.5 hot-reload — council confirms out
- [ ] §7.6 cross-language contract test — council decides
- [ ] §7.7 D22/D24 refresh removable — verify in shadow phase
- [ ] Spike: Elixir on Pi, single sensor (BMP280) GenServer reading and emitting telemetry. ~3 days. Promotes RFC to v0.1 if the spike surfaces gaps.
- [ ] Cross-link with `surface-lease-plane-v0.md` Phase B status before starting Phase A here. Lease plane is the substrate trust-build; this RFC inherits that trust.

## 10. Versions / changelog

- v0 (2026-04-30) — initial draft. Pre-council. Authored after archaeology session that surfaced existing operator decision for BEAM-as-destination (KG `2026-04-30T19:30:54.644112+00:00`).
