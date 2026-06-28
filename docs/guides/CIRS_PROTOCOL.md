# CIRS Protocol — Multi-Agent Coordination

Status: specialized protocol reference. Use when working on CIRS-specific coordination flows, not as a general architecture overview.

**Cooperative Inter-agent Resonance Signaling**

CIRS enables agents to broadcast state, coordinate recovery, and establish trust boundaries. It builds on the EISV vocabulary to let agents observe and respond to each other's governance state.

---

## Overview

CIRS has five protocols, each handling a different coordination concern:

| Protocol | Purpose | Actions |
|----------|---------|---------|
| **void_alert** | Broadcast void state warnings | `emit`, `query` |
| **state_announce** | Share EISV + trajectory with peers | `emit`, `query` |
| **coherence_report** | Compute pairwise agent similarity | `emit`, `query` |
| **boundary_contract** | Define trust policies between agents | `set`, `get`, `list` |
| **governance_action** | Coordinate governance requests | `emit`, `query` |

All protocols are accessed via the `cirs_protocol` tool:
```
cirs_protocol(protocol="void_alert", action="emit")
cirs_protocol(protocol="boundary_contract", action="set", ...)
```

---

## Protocols

### 1. Void Alert

Broadcasts when an agent enters void state (|V| exceeds threshold).

**Emit:**
```
cirs_protocol(protocol="void_alert", action="emit", severity="warning|critical")
```
- `warning`: Agent approaching void threshold
- `critical`: Agent deep in void state

**Query:**
```
cirs_protocol(protocol="void_alert", action="query")
```
Returns recent void alerts from all agents.

### 2. State Announce

Broadcasts full EISV state plus trajectory metadata to peers.

**Emit:**
```
cirs_protocol(protocol="state_announce", action="emit")
```
Includes: E, I, S, V values, coherence, risk trend, regime, verdict, and trajectory signature (maturity, convergence, decision bias, focus stability).

**Query:**
```
cirs_protocol(protocol="state_announce", action="query")
```
Returns recent state announcements from all agents.

### 3. Coherence Report

Computes pairwise similarity between agents.

**Emit:**
```
cirs_protocol(protocol="coherence_report", action="emit")
```
Calculates similarity against all recently active agents using weighted EISV comparison: 25% E, 35% I, 25% S, 15% V, plus regime match, verdict match, and trajectory similarity.

**Query:**
```
cirs_protocol(protocol="coherence_report", action="query")
```
Returns recent coherence reports.

### 4. Boundary Contract

Defines trust policies and void response rules between agents.

**Set:**
```
cirs_protocol(protocol="boundary_contract", action="set",
  target_agent_id="...",
  trust_level="full|partial|observe|none",
  void_policy="notify|assist|isolate|coordinate")
```

Trust levels:
| Level | Meaning |
|-------|---------|
| `full` | Full coordination and data sharing |
| `partial` | Limited coordination |
| `observe` | Read-only observation |
| `none` | No interaction |

Void policies:
| Policy | Meaning |
|--------|---------|
| `notify` | Alert when neighbor enters void |
| `assist` | Actively help neighbor recover |
| `isolate` | Reduce interaction with void neighbor |
| `coordinate` | Joint recovery coordination |

**Get/List:**
```
cirs_protocol(protocol="boundary_contract", action="get", target_agent_id="...")
cirs_protocol(protocol="boundary_contract", action="list")
```

### 5. Governance Action

Broadcasts governance coordination requests (recovery conditions, pauses, state sync).

**Emit:**
```
cirs_protocol(protocol="governance_action", action="emit", ...)
```

**Query:**
```
cirs_protocol(protocol="governance_action", action="query")
```

---

## Auto-Emit Hooks

Most CIRS signals are emitted automatically during `process_agent_update()`. You don't need to call them manually:

| Hook | When it fires |
|------|---------------|
| Void-alert helper | After check-in if |V| > threshold |
| State-announce helper | After every check-in |
| Resonance-signal helper | If neighbor agents have similar state |

Additional standalone signals:
- `resonance_alert` — emitted when multi-agent resonance detected
- `stability_restored` — emitted when agent exits unstable state

---

## When to Use CIRS Manually

Most agents never need to call CIRS directly — the auto-emit hooks handle broadcasting. Manual use is for:

1. **Setting boundary contracts** — Define how you want to interact with specific agents
2. **Querying state** — Check what other agents are broadcasting
3. **Custom coordination** — Governance actions for multi-agent workflows

---

## Handler Modules

Source code in `src/mcp_handlers/cirs/`:

| Module | Purpose |
|--------|---------|
| `protocol.py` | Consolidated entry point (routes to sub-handlers) |
| `void.py` | Void alert emit/query |
| `state.py` | State announce + trajectory computation |
| `coherence.py` | Coherence report + similarity math |
| `boundary.py` | Boundary contract CRUD |
| `governance_action.py` | Governance action broadcast |
| `resonance.py` | Resonance alert + stability restored signals |
| `hooks.py` | Auto-emit hooks called during check-in |
