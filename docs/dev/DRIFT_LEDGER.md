# Drift Ledger

**One index for the guards that keep the layers honest — and the seams that have none.**

Status: live operational index. If this file and runtime code disagree, trust [CANONICAL_SOURCES.md](CANONICAL_SOURCES.md) and the referenced runtime files. For the end-to-end architecture prose, see [UNIFIED_ARCHITECTURE.md](../UNIFIED_ARCHITECTURE.md).

The system has many components but few channels. This page exists so the
anti-drift guards — which are real but scattered across `scripts/dev/`,
`agents/`, and CI — have a single place to be looked up, and so unguarded
seams stay visible instead of being rediscovered each incident.

## Topology: hub-and-spoke, not a mesh

```
  Clients (Claude / Codex / SDK agents)
         │  MCP Streamable HTTP (:8767)
         ▼
  ┌──────────────────────┐      asyncpg (ExecutorPool)   ┌─────────────────┐
  │   MCP server (hub)    │ ───────────────────────────▶ │ PostgreSQL+AGE  │  ← source of truth
  │  dispatch_tool →      │ ◀─────────────────────────── │ (identity,audit,│
  │  middleware → handler │                              │  KG, sessions)  │
  └──────────────────────┘                              └─────────────────┘
     ▲   │  /ws/eisv            Redis (optional, sticky session cache)
     │   ▼
  Agents (observers): Watcher · Sentinel · Vigil · Chronicler
     read state via /ws/eisv + REST /v1/tools/call,
     write findings back as KG notes + audit events + local JSONL
```

Key property: agents never talk to each other directly. Everything rendezvous
through the hub and the DB. ~51 tools auto-register through one `@mcp_tool`
decorator and one dispatch path, so there is one registration truth, not one
per handler.

## Communication channels

| Channel | Between | Carries |
|---|---|---|
| MCP Streamable HTTP (`:8767`) | clients ↔ hub | tool calls / results |
| asyncpg via `ExecutorPool` | hub ↔ PostgreSQL+AGE | identity, audit trail, KG |
| Redis (optional) | hub ↔ cache | sticky client→agent binding, metadata TTL |
| WebSocket `/ws/eisv` | hub → Sentinel | real-time EISV event stream |
| REST `/v1/tools/call` | agents → hub | check-ins, KG writes |
| local files (gitignored) | agent ↔ itself | `data/watcher/`, `data/*_state`, `~/.unitares/anchors/` |

## Drift ledger

Each row is a place two things must stay in agreement. "Gate" is what fails
when they don't.

| Seam | What can drift | Guard | Gated? |
|---|---|---|---|
| `db/postgres/migrations/` | slot/name drift, schema ↔ applied DB | `scripts/dev/unitares_doctor.py` | ✅ CI |
| AGENTS.md ↔ CLAUDE.md | the SHARED CONTRACT block | `scripts/dev/check-shared-contract.sh` | ✅ CI |
| code ↔ tests | regressions | `scripts/dev/test-cache.sh` (tree-hash) | ✅ pre-push + CI |
| docs ↔ runtime | stale architecture phrasing, tool count, version | `scripts/diagnostics/check_doc_drift.py`, `documentation-validation.yml` | ✅ CI |
| tool schema ↔ tool modes | served surface vs config | `scripts/diagnostics/validate_tool_modes.py` | ✅ CI |
| `skills/` ↔ committed fingerprint | a skill edited without republishing the fingerprint | `scripts/dev/skills_manifest.py --check` | ✅ CI (smoke) |
| `unitares/skills/` ↔ plugin `skills/` mirror | content divergence across repos | fingerprint above lets the plugin verify cheaply; full diff is `sync-plugin-skills.sh --check` | ⚠️ canonical side only — **plugin-side CI comparison still UNGUARDED** |
| plugin hook chain liveness | hooks silently going dark | `agents/vigil/checks/plugin_hook_liveness.py` | ⚠️ external canary, not a CI gate |
| identity/onboarding docs ↔ code (cross-repo) | coupled changes diverging | single-writer-surface rules in the SHARED CONTRACT | ❌ human discipline |
| vocabulary homonyms ("substrate", "fingerprint", "surface") | same word, different meaning | `docs/ontology/glossary-drift-audit-2026-06-20.md` | ❌ audit doc, no gate |
| parallel sessions on a single-writer surface | colliding branches | `gh pr list` check before starting (SHARED CONTRACT) | ❌ human discipline |

## How to extend this

- New guard? Add a row with its gate, and prefer wiring it into CI's DB-free
  `smoke` job (`.github/workflows/tests.yml`) next to the other validators.
- New seam with no guard yet? Add a row marked **UNGUARDED** so it is visible
  rather than tribal knowledge. Closing the most live one is usually higher
  leverage than new architecture.
