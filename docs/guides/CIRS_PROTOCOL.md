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
| **coherence_report** | Compute pairwise agent similarity | `compute`, `query` |
| **boundary_contract** | Define trust policies between agents | `set`, `get`, `list` |
| **governance_action** | Coordinate governance requests | `initiate`, `respond`, `query`, `status` |

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

Omit `severity` to have it detected from the void threshold; the call is refused when |V| is below it. `context_ref` attaches a free-form reference (a task, file or ticket).

**Query:**
```
cirs_protocol(protocol="void_alert", action="query")
```
Returns recent void alerts from all agents. Filters: `filter_agent_id`, `filter_severity` (`warning` or `critical`), `since_hours` (default 1.0) and `limit` (default 50).

### 2. State Announce

Broadcasts full EISV state plus trajectory metadata to peers.

**Emit:**
```
cirs_protocol(protocol="state_announce", action="emit")
```
Includes: E, I, S, V values, coherence, risk trend, regime, verdict, and trajectory signature (maturity, convergence, decision bias, focus stability). Pass `include_trajectory=false` to leave the signature out.

**Query:**
```
cirs_protocol(protocol="state_announce", action="query")
```
Returns recent state announcements from all agents. Filters: `agent_ids`, `regime` (`divergence`, `transition`, `convergence` or `stable`), `max_risk` and `limit` (default 50). `min_coherence` is retired and refused with `UNSUPPORTED_COHERENCE_FILTER`.

### 3. Coherence Report

Computes pairwise similarity between agents.

**Compute:**
```
cirs_protocol(protocol="coherence_report", action="compute", target_agent_id="...")
```
Calculates similarity against one named agent using weighted EISV comparison: 25% E, 35% I, 25% S, 15% V, plus regime match, verdict match, and trajectory similarity. `target_agent_id` is required. This protocol has no `emit` action.

**Query:**
```
cirs_protocol(protocol="coherence_report", action="query")
```
Returns recent coherence reports. Filters: `source_agent_id`, `target_agent_id`, `min_similarity` and `limit` (default 50).

### 4. Boundary Contract

Defines trust policies and void response rules between agents.

**Set** your own contract:
```
cirs_protocol(protocol="boundary_contract", action="set",
  trust_default="full|partial|observe|none",
  trust_overrides={"<agent_id>": "full|partial|observe|none"},
  void_response_policy="notify|assist|isolate|coordinate",
  max_delegation_complexity=0.5,
  accept_coherence_threshold=0.4)
```
Every parameter is optional; the values shown for the last two are the defaults, and `trust_default` / `void_response_policy` default to `partial` / `notify`. The contract is yours, so `set` takes no `target_agent_id`: trust toward one named agent goes in `trust_overrides`. Earlier revisions of this guide named `trust_level` and `void_policy`, which the handler never read, so a call written that way stored `partial` and `notify` whatever it asked for.

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

**Initiate:**
```
cirs_protocol(protocol="governance_action", action="initiate", action_type="void_intervention", target_agent_id="...")
```
`action_type` is one of `void_intervention`, `coherence_boost`, `delegation_request`, `delegation_response` or `coordination_sync`; `payload` carries free-form data. A target whose boundary contract gives you trust `none` refuses the initiation.

**Respond** to an action addressed to you, and read one back:
```
cirs_protocol(protocol="governance_action", action="respond", action_id="...", accept=True)
cirs_protocol(protocol="governance_action", action="status", action_id="...")
```
`accept` defaults to false, so a respond without it rejects; `response_data` carries free-form data back.

**Query:**
```
cirs_protocol(protocol="governance_action", action="query")
```
Returns actions you initiated and actions targeting you. Narrow with `as_initiator=false`, `as_target=false` or `status_filter` (`pending`, `accepted` or `rejected`).

This protocol has no `emit` action.

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
