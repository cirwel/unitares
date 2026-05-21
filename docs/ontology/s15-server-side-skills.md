# S15 — Server-side skills surface

**Date:** 2026-04-25
**Scope:** Plan doc for promoting `skills/` from a per-client artifact to a server-authored surface, addressed via the MCP wire protocol. Inventories the current drift between `unitares/skills/` and `unitares-governance-plugin/skills/`, motivates the change with a concrete cross-client failure (Hermes-driven proposal to auto-inject `continuity_token` at the client transport layer, 2026-04-25), and lays out a sequenced delivery path.
**Stance:** Descriptive + recommendation. No code changes in this pass.
**Unblocks:** Cross-client governance literacy. Reduces operator burden of N skill bundles per N clients. Closes the structural gap that allowed Hermes to propose a removed-architecture "fix" against UNITARES.
**Tightly coupled to:** S1 (continuity_token narrowing — agents that don't know v2 ontology re-derive the wrong fix), S5 / S11 / S13 (lineage-declaration teaching surface), S8a (tag-discipline — same write-path-drift class of problem), and the existing `describe_tool` / `list_tools` introspection surface.
**Review provenance:** drafted 2026-04-25, reviewed in one pass by `dialectic-knowledge-architect` (ontology stress-test) and `feature-dev:code-reviewer` (call-site accuracy) before landing. Key review findings folded in: TL;DR softened to honest scope (Hermes incident closes at S15-e, not S15-a); §1a s8a-analogy weakened in favor of the §2.3 N×M-scaling argument as the actual structural carry; §4.5 gained a cache-key ontology guard (never key skill cache by agent_uuid); §4.6 + Appendix corrected to acknowledge the `__init__.py` registration line and the `rate_limit_step.py` `read_only_tools` entry; §6 added as a new "tool descriptions are authoritative for tool-call invariants; skills supplement, never contradict" invariant + complementary cheap-cure path (embed v2 ontology in `src/tool_descriptions.json`); §7.4 split auth (truly unchanged) from rate-limit parity (needs one-line middleware update) and added an authz-non-encoding guard; §7 risks gained an MCP-liveness coupling note that bites at S15-c/d, not S15-a; §10 gained the canonical-on-server-is-one-way decision point. Test filenames + frontmatter example corrections folded into the Appendix.

## TL;DR

Skills today are **client-side artifacts authored once per client format**. The Claude Code plugin ships `unitares-governance-plugin/skills/`; the unitares server repo ships a partially-synchronized copy at `unitares/skills/`. As of 2026-04-25, **5 of 6 shared skills differ between the two repos** (verified by `diff -rq`), and `unitares-dashboard` exists only in the plugin. Codex consumes the plugin bundle. claude.ai has no skill surface — only MCP tools. Hermes has no skill bundle at all.

This produces three structural failures:

1. **Drift across clients.** v2-ontology updates land in one repo and decay in the other. The 2026-04-17 identity-honesty work is taught accurately in Claude Code skills but invisibly absent from any client without a synchronized bundle.
2. **Format lock-in.** Each client ships its own skill format (Claude Code plugin manifest, Codex commands, claude.ai connectors, Hermes — undefined). Adding a new client = N new skill bundles to maintain.
3. **Anti-pattern construction by the model.** When a client lacks a skill bundle, the model derives behavior from tool schemas alone. The 2026-04-25 Hermes incident — proposing to auto-inject `continuity_token` at the transport layer — is exactly the failure mode: a smart model with correct tool schemas, no skill content, and an invariant (Identity Honesty Part C, 2026-04-18) that exists only in the skill content.

**Recommendation:** ship a server-side `skills` MCP surface. Server is source of truth. Clients consume canonical content via MCP and render in their native skill format via thin adapters. Sequenced as **S15-a (server surface) → S15-b (canonical content consolidation) → S15-c (per-client adapters) → S15-d (plugin shim)**. S15-a is the structural-fix first ship; **S15-b should be at least drafted before S15-a lands** so the server tool does not begin life pointing at a known-drifted source-of-truth (analog of S1's TTL-shrink-paired-with-secret-rotation operator step).

**Honest scope of what S15-a alone closes.** S15-a closes the structural drift problem on the server side and gives Claude Code (already a working consumer) a canonical content path. **The Hermes 2026-04-25 incident is not closed by S15-a** — it closes at S15-e, which depends on Hermes-side adapter work this design does not own. The TL;DR previously over-claimed; the honest framing is that S15-a is necessary but not sufficient for the originating incident, and §6 below identifies a cheaper *complementary* path (embed v2-ontology invariants in `src/tool_descriptions.json`) that closes the immediate Hermes-class failure without waiting for adapter work.

## 1. Current state inventory

### 1a. Skill bundles (content)

**`unitares/skills/`** (server repo, 6 skills):
- `dialectic-reasoning/SKILL.md`
- `discord-bridge/SKILL.md`
- `governance-fundamentals/SKILL.md`
- `governance-lifecycle/SKILL.md`
- `knowledge-graph/SKILL.md`
- `unitares-governance/SKILL.md`

**`unitares-governance-plugin/skills/`** (plugin repo, 7 skills):
- All six above (each differs from the unitares-side counterpart per `diff -rq`)
- `unitares-dashboard/SKILL.md` — plugin-only

**Drift evidence (2026-04-25):**
```
$ diff -rq unitares/skills/ unitares-governance-plugin/skills/
Files .../dialectic-reasoning/SKILL.md and .../dialectic-reasoning/SKILL.md differ
Files .../discord-bridge/SKILL.md and .../discord-bridge/SKILL.md differ
Files .../governance-fundamentals/SKILL.md and .../governance-fundamentals/SKILL.md differ
Files .../governance-lifecycle/SKILL.md and .../governance-lifecycle/SKILL.md differ
Files .../knowledge-graph/SKILL.md and .../knowledge-graph/SKILL.md differ
Only in .../unitares-governance-plugin/skills: unitares-dashboard
Files .../unitares-governance/SKILL.md and .../unitares-governance/SKILL.md differ
```

This is not a problem to be solved by careful syncing. The two-repo case alone could in principle be fixed by sync CI (Option D below), so the structural argument here is *not* the s8a "audit-after-the-fact can't catch write-path policy violations" lesson — that's a different shape of problem. The actual structural argument lands on the **N×M scaling case** (§2 point 3): each new client adds another bundle, each ontology change requires N edits, and disciplined sync stops mattering at that scale. The `diff -rq` snapshot above was taken 2026-04-25; rerun immediately before S15-a opens for current-state accuracy (one of the six pairs may have converged on `last_verified` since).

### 1b. Existing introspection surface

The server already exposes tool-shape introspection:
- `src/mcp_handlers/introspection/tool_introspection.py` — `describe_tool` (handler `handle_describe_tool`, L1010), `list_tools` (handler `handle_list_tools`, L52). MCP-wire names registered via `@mcp_tool` decorator.
- `src/tool_descriptions.json` — canonical tool descriptions (used by `describe_tool` to render content; see §6 for why this file is also a load-bearing teaching surface).

Skills are conceptually a sibling surface: tool-shape tells you *what to call*; skills tell you *when to call it and what invariants to honor*. They share the same authority pattern (server-authored, client-rendered) but are not currently exposed as a model-readable structured response.

**Note on `src/mcp_handlers/introspection/export.py`:** the file lives in the `introspection/` directory but contains governance-history export handlers (`get_system_history`, `export_to_file`), not tool/registry introspection. Co-location is an accident of directory naming. The S15-a implementer should **not** treat `export.py` as a structural model — the introspection peer is `tool_introspection.py`, full stop.

### 1c. Frontmatter conventions already in skill files

Existing `SKILL.md` files use a YAML frontmatter pattern that anticipates server-side authority:

```yaml
---
name: governance-lifecycle
description: >
  Use when an agent is interacting with UNITARES governance for the first time...
last_verified: "2026-04-25"
freshness_days: 14
source_files:
  - unitares/src/mcp_handlers/core.py
  - unitares/src/mcp_handlers/identity/handlers.py
  - unitares/src/mcp_handlers/admin/handlers.py
# (excerpt — real files have 3+ source_files entries; schema parser
#  must accept N entries, not exactly two)
---
```

`last_verified` + `freshness_days` + `source_files` is staleness-tracking infrastructure that today only the operator reads. Once skills go server-side, the server can compute freshness against `git log` of `source_files` and surface a `stale: true` flag in the response — operators stop chasing skills that drifted with the code.

### 1d. Hermes 2026-04-25 incident (motivating example)

A Hermes-driven session, lacking a UNITARES skill bundle, observed that `identity()` calls without explicit `resume=true` mint fresh identities. The model classified this as a Hermes-client bug and proposed the "fix": auto-inject `continuity_token` between calls at the MCP client layer. The proposal was saved as a "high severity" knowledge-graph discovery.

This is **diametrically opposed to the architecture** (Identity Honesty Part C, 2026-04-18, Invariant #4 closure):
- `force_new=true` is the v2 default posture; lineage is declared via `parent_agent_id`, not resumed via token
- Auto-injection would re-introduce the silent-resurrection vector S1 is narrowing
- The KG entry now teaches the same anti-pattern to any future agent that searches for "continuity_token"

The model was operating correctly given its information set. The information set was wrong. This is not a model-quality problem; it is a teaching-surface problem. Server-side skills close it.

## 2. Why the existing two-repo pattern is structurally insufficient

Three forces produce the drift, and none of them are fixed by discipline:

1. **Source-of-truth ambiguity.** When skills live in both repos, neither is canonical. Edits land wherever the operator happens to be working.
2. **No mechanical sync.** There is no CI check asserting `unitares/skills/` ≡ `unitares-governance-plugin/skills/` byte-for-byte (compare with the `check-shared-contract.sh` parity check between `AGENTS.md` and `CLAUDE.md` — that one *is* enforced, and consequently does not drift).
3. **N×M scaling.** Adding a new client adds an Mth skill bundle. Updating an ontology rule requires N edits. The cost grows multiplicatively.

The same logic that motivated MCP itself — "tools should be server-authored, not re-implemented per client" — applies one level up to skills.

## 3. Options

| # | Description | Tradeoff |
|---|---|---|
| **A. Server-side `skills` MCP tool** | New `skills` tool returning `{index, content}` keyed by skill name + version. Clients call once on session start; render the markdown in their native skill mechanism. Content lives in `unitares/skills/` (source of truth). Plugin's bundle becomes a thin generated mirror. | Single source of truth; trivial schema; no MCP-spec changes. Costs: per-client adapter work, activation-trigger portability problem (see §5). |
| **B. Server-side skills as MCP `resources`** | Use the MCP `resources` primitive: `unitares://skills/<name>` returns markdown content. `unitares://skills/index` lists. | More spec-aligned long-term; resources are designed for server-published documents. Costs: many MCP clients today don't auto-load resources into model context — they require explicit `read_resource` calls. Adapter burden similar to Option A but with extra "hook to auto-fetch on session start" work. |
| **C. Hybrid — tool for index, resources for content** | `skills` tool returns the index (cheap, called once at session start). Full content fetched via `resources` on-demand when a client wants to render a specific skill. | Bandwidth-efficient. Costs: more moving parts; two surfaces to keep in sync. Premature optimization for current scale (~7 skills, < 100KB total). |
| **D. Status quo (operator-disciplined sync)** | Keep two repos; add a `check-skills-parity.sh` CI gate. Manual sync continues. | Zero new infrastructure. Costs: every drift incident still happens; doesn't address Hermes / claude.ai / future-client bundles at all. The sync gate solves *one* drift pair, not the N-client class of problem. |

**Recommendation:** **A** as the first ship. Smallest change, highest leverage, ships in one PR. Resource-style (B) becomes a follow-on if MCP clients converge on auto-loading resources — the `skills` tool can coexist with a future `unitares://skills/` resource surface emitting the same content.

**Why not C now:** the bandwidth argument doesn't yet bite (skills total ~50KB markdown). Unifying the index + content into a single `skills` tool response avoids the two-surface sync cost.

## 4. Scope under Option A

### 4.1. Server-side surface

New tool: `skills(name?: string, since_version?: string)` → returns:

```json
{
  "skills": [
    {
      "name": "governance-lifecycle",
      "description": "...",
      "version": "2026-04-25",
      "content_hash": "sha256:...",
      "last_verified": "2026-04-25",
      "freshness_days": 14,
      "stale": false,
      "source_files": ["unitares/src/mcp_handlers/core.py", "..."],
      "triggers": {
        "keywords": ["onboard", "checkin", "session_start", "lineage"],
        "tool_calls": ["mcp__unitares-governance__onboard", "mcp__unitares-governance__bind_session"],
        "situations": "agent is interacting with UNITARES governance for the first time, needs to onboard, check in, or recover from a pause/reject verdict"
      },
      "content": "# Agent Lifecycle\n\n..."
    }
  ],
  "registry_version": "2026-04-25",
  "registry_hash": "sha256:..."
}
```

Three flag-style options on the request:
- `name=<skill-name>` returns single skill
- `since_version=<date>` returns only skills updated since (cheap re-poll)
- absent: returns full bundle (default — typical session-start case)

### 4.2. Canonical content location

Source of truth: **`unitares/skills/`**. Plugin's bundle (currently in `unitares-governance-plugin/skills/`) becomes a generated mirror, populated by a `scripts/dev/sync-plugin-skills.sh` script that the plugin's build runs. CI gate added on the plugin side: "the bundle must be in sync with the unitares server repo at the SHA tagged in plugin manifest."

### 4.3. Activation triggers (the load-bearing field)

Every skill currently has a free-text `description:` field that Claude Code matches heuristically. **This does not travel.** Codex needs explicit `commands` mapping; Hermes (per the 2026-04-25 conversation) needs system-prompt injection; claude.ai has no skill mechanism at all and would consume skills as context-injected text.

The `triggers:` field in §4.1's payload is the cross-client common denominator:
- `keywords` — for clients that match by user-message content (Hermes-style)
- `tool_calls` — for clients that activate skills before specific tool invocations (the strongest signal)
- `situations` — free-text fallback for clients with capability for situational matching

Each adapter maps `triggers` to its native activation mechanism. Without this field, server-side skills ship correct content that never fires.

### 4.4. Freshness flag

Server computes `stale: true` if `now - last_verified > freshness_days`. Cheap to compute. Adapters surface staleness in their UI ("⚠️ this skill was last verified more than X days ago"). Closes the most common drift class without any operator action.

### 4.5. `registry_version` + `registry_hash`

Top-level fields on the response. Adapters cache the registry; on next session start, they call `skills(since_version=<cached>)` and only re-fetch if changed. Cheap polling for a registry that doesn't change often.

**Identity-blindness invariant (ontology guard).** Skills content and the registry cache **must not be keyed by `agent_uuid` or any identity-derived value**. The skill cache is process-instance-local, content-addressed, and identity-independent. A future "let's personalize skill content per-agent" optimization would import identity into a content surface that is structurally identity-blind by design — same category of error as the deleted `resolve_by_name_claim` primitive (cosmetic-attribute keying for what should be content-addressed lookup). Adapters that violate this should fail a parametrized contract test in the relevant adapter repo.

### 4.6. What this PR does NOT do

- Touch any existing skill *content* (`unitares/skills/*/SKILL.md` or `unitares-governance-plugin/skills/*/SKILL.md`)
- Add per-client adapters (S15-c)
- Migrate the plugin bundle to be a generated mirror (S15-d)
- Define how Hermes / claude.ai / Codex render skills (out of unitares server scope; each adapter's home repo owns it)
- Change `describe_tool` / `list_tools` semantics

**What this PR DOES touch beyond the new files** (single-concern, but not zero-touch):
- `src/mcp_handlers/__init__.py` — one new import line to register `skills.py`. The `TOOL_HANDLERS` dict is built from `get_decorator_registry()` at import time; a new file is not picked up unless explicitly imported. This is mechanical, not a logic change.
- `src/mcp_handlers/middleware/rate_limit_step.py` L53 — add `'skills'` to the `read_only_tools` set (see §7.4 for why; the `rate_limit_exempt` decorator metadata is not currently consulted by the middleware, so parity with `list_tools` rate-limit behavior requires this set entry).

Single-concern in spirit (shipping the server surface, no cross-cutting logic changes). The two files above are bookkeeping; flag them in the PR body, not as scope creep.

## 5. Cross-client portability — the activation-trigger problem

The *content* of skills is portable. Markdown is markdown. The hard problem is **when does each client decide to use a skill**.

| Client | Native activation mechanism | Trigger source |
|---|---|---|
| Claude Code | `Skill` tool + frontmatter `description:` | Model heuristically matches user message + tool-call context against descriptions |
| Codex | Slash commands (`commands/*.md`) | User explicitly invokes |
| claude.ai | None — no skill surface | Skills must be prepended to system prompt as context |
| Hermes | Undefined | TBD per Hermes adapter |

A `triggers` field with three sub-fields (`keywords`, `tool_calls`, `situations`) is the minimum cross-client common denominator. Each adapter chooses how to honor it:
- Claude Code adapter ignores `keywords` and `tool_calls`, generates a `description:` from `situations` for the existing heuristic
- Codex adapter generates `commands/*.md` files where `tool_calls` matches translate to slash-command suggestions
- claude.ai adapter prepends all skills to system prompt unconditionally (no triggers honored — context budget permitting)
- Hermes adapter (per the 2026-04-25 conversation, system-prompt injection) prepends skills whose `keywords` match the current message

This is **lossy**. Different clients honor different subsets. That is acceptable as long as the loss is documented per-client. The alternative — a single trigger language honored everywhere — is a research-grade problem.

**Audit-surface caveat (lossy-but-documented vs. lossy-and-invisible).** The "documented loss" framing only holds if there is downstream telemetry surfacing *"skill X expected to fire in situation Y but did not fire on client Z."* Without such an audit, lossy-but-documented degrades into lossy-and-invisible — the same agent instance honors different invariants across runtimes, and operators have no way to tell. S15-a's response payload should carry enough metadata (skill versions, trigger schema, registry hash) that a future activation-audit tool can correlate "skill should have fired" with "skill did fire." Concrete shape TBD; flagged as an S15-g forcing-function rather than S15-a scope.

## 6. Tool descriptions vs. skills — authority hierarchy

The Hermes 2026-04-25 incident (§1d) is the motivating example for this design, but it also surfaces a complementary path that is cheaper than the full S15-a/c/e wave: **embed v2-ontology invariants directly in `src/tool_descriptions.json`**.

Tool descriptions are MCP-canonical — every client honors them, no portability problem, no activation-trigger problem, no adapter work. If `onboard`'s description carried the v2-ontology summary (*"v2 default: pass `force_new=true` with `parent_agent_id=<prior UUID>` for new process-instances; do not auto-inject `continuity_token` between calls — that is a removed silent-resurrection vector closed by Identity Honesty Part C, 2026-04-18"*), the Hermes incident does not happen, with zero new tool surface.

This is **not a substitute for S15** — the surfaces serve different purposes:

| Surface | Authority for | Activation |
|---|---|---|
| `tool_descriptions.json` (existing) | Invariants attached to specific tool calls | Always loaded by every MCP client; no adapter work |
| Server-side skills (S15) | Situational/procedural knowledge ("how to check in", "when to invoke dialectic") | Adapter-mediated; activation triggers vary by client |

**Authority invariant (load-bearing for governance literacy):** **tool descriptions are authoritative for tool-call invariants; skills supplement, never contradict.** If skill content describes an invariant attached to a specific tool call, the tool description is the source of truth and the skill paraphrases. If they conflict, the tool description wins. A CI check on this is feasible long-term (parse skills, find tool-call references, diff against `tool_descriptions.json` claims) but out of scope for S15-a.

**Concrete recommendation paired with S15-a:** audit `src/tool_descriptions.json` for the top 3-5 most-removed-architecture invariants (no auto-inject token; never call `onboard()` argless from cached identity; lineage via `parent_agent_id` not `continuity_token`; `client_session_id` is within-process only; substrate-earned identity vs. session-like routing). Embed each as a one-line "DO NOT" or "v2 posture" clause in the relevant tool's description. This is a 1-PR change that closes the immediate Hermes-class incident without waiting for adapter work — and it benefits clients (claude.ai, future MCP consumers) that may never get a skills adapter.

The tool-description audit is **not blocking S15-a** but is the highest-leverage governance-literacy fix available right now. Operator decision: ship before, after, or alongside S15-a.

## 7. Versioning and staleness

Three layers:

1. **Per-skill version** = ISO date string from `last_verified:` field. Bumps when content edits land.
2. **Registry version** = max of all per-skill versions. Bumps on any edit.
3. **Registry hash** = sha256 of canonical-ordered registry. Used by adapters to detect tampering / unexpected drift.

Cache invalidation is "if `registry_version` ≠ cached, re-fetch." Adapters call `skills(since_version=<cached>)`; server returns only deltas (typically empty).

**Staleness signal** flows from `source_files`. If any file in `source_files` has commits after `last_verified`, the skill is potentially stale. This is computed by the server on read, not stored. Cheap (`git log --since` is O(log n)).

## 8. Risks and breakage

### 8.1. Acceptable

- **Server-fetch latency on session start.** One extra round-trip. Skills bundle is < 100KB. Trivial.
- **Plugin bundle goes from "manually authored" to "generated mirror."** Plugin contributors can't edit skills directly anymore — must edit unitares server repo. Acceptable; matches `governance_core` 2026-04-24 fold-back lesson (single source of truth wins).
- **Stale-cache window during deployment.** Adapters with cached registries see old content until next session start. Acceptable; staleness is bounded by typical session length.

### 8.2. Re-onboard on registry version bump

If a client honors `registry_version` aggressively, every skill edit bumps the version and forces re-fetch on every active session. Could be noisy if skill edits are frequent. Mitigation: skill edits are rare (skill bundles change weekly, not hourly); cost is negligible.

### 8.3. Trigger-language drift

If `triggers` schema evolves (e.g., adding `excluded_tools` for negative matching), older adapters silently ignore new fields. Forward-compat: all adapters must tolerate unknown fields in `triggers`. Tested via a parametrized contract test on each adapter.

### 8.4. Authorization and rate-limit posture

**Auth (transport-level): no new surface.** `skills` is a read-only introspection tool, same bearer-token / IPUA posture as `list_tools` / `describe_tool`. The auth claim is unchanged from the original draft.

**Rate-limiting: needs explicit middleware entry.** The `rate_limit_exempt=True` flag on the `@mcp_tool` decorator (used by both `list_tools` and `describe_tool`) is stored in `ToolDefinition` but **not currently consulted** by `src/mcp_handlers/middleware/rate_limit_step.py`. Rate-limit exemption is determined by a hardcoded `read_only_tools` set at `rate_limit_step.py:53`. `describe_tool` is **not** in that set today (pre-existing gap that affects `describe_tool` too, not S15-introduced). For `skills` to inherit `list_tools`'s rate-limit behavior — read-only, exempt — the implementer must add `'skills'` to that set. One-line change; flagged in §4.6 as expected scope. Fixing the broader "decorator metadata not consulted" gap is out of scope for S15-a.

**Authz-non-encoding invariant (load-bearing).** Skills content **must not encode authorization**. Skills *describe* tool-call invariants and procedures; they **do not enforce** access control. If a future operator is tempted to write *"only admin agents may call `archive_orphan_agents`"* in a skill, the right place is the handler — skill content is advisory and adapter-mediated, and adapters may render or ignore it independently of governance state. Same shape as the s8a write-path-vs-audit lesson (policy enforced in audit can be subverted at write). A regression test on `skills` content (CI lint) checking for forbidden imperative-permission language ("only X can", "must be admin") is feasible long-term; out of scope for S15-a.

### 8.5. The CLAUDE.md / AGENTS.md content overlap

Bootstrap context (CLAUDE.md, AGENTS.md, CODEX_START.md) is a separate surface — loaded by the client at session start before MCP is reachable. Server-side skills cannot replace it.

**But content overlaps.** `unitares/CLAUDE.md`'s "Minimal Agent Workflow" section teaches v2 ontology; `unitares/skills/governance-lifecycle/SKILL.md` teaches the same lifecycle; `unitares/skills/governance-fundamentals/SKILL.md` covers EISV/coherence basics that overlap with bootstrap. Server-side skills shipping as canonical does **not** fix this drift class — it relocates it. The same v2-ontology lesson can drift between CLAUDE.md (file-on-disk in the bootstrap surface) and `unitares/skills/governance-lifecycle/` (server-canonical post-onboard).

**Reconciliation followup (S15-b scope, not S15-a):** in S15-b's content consolidation pass, decide which content lives where and ideally make CLAUDE.md a *pointer-to-skills* ("the canonical lifecycle is at `skills/governance-lifecycle`; this section is a stub for first-onboard before MCP is reachable") rather than a duplicated mini-skill. CLAUDE.md retains only the irreducible bootstrap content (toolchain setup, repo conventions, what-not-to-reference); ontology and lifecycle move to skills entirely.

### 8.6. MCP-liveness coupling at S15-c/d

S15-a is purely additive — adapters that don't yet exist can't be coupled to MCP liveness. **The coupling enters at S15-c.** Once the Claude Code adapter gains "fetch from server on session start, fall back to bundled mirror on offline," skill rendering becomes a session-start dependency on MCP reachability. If MCP is briefly unreachable (cloudflare tunnel hiccup, governance-mcp restart in progress, anyio-asyncio deadlock — see CLAUDE.md "Known Issue"), the model bootstraps **without skills** — the same information-set hole the Hermes incident exposed.

Mitigation: S15-c's "fall back to bundled mirror on offline" path is the safety net. After S15-d ("plugin bundle becomes generated mirror"), the bundled mirror remains as a generated artifact on disk — not human-edited, but present and readable during transient MCP outages. The fallback teaches *known-stale* content during outages, bounded by plugin install age.

This is honest acknowledgment, not a blocker. S15-a alone does not introduce the coupling; flagging here so S15-c/d PRs cite this section and thread "offline fallback" as an explicit requirement, not an afterthought.

### 8.7. claude.ai context-budget impact

If the claude.ai adapter prepends all skills to system prompt, that's ~50KB of context cost on every conversation. May bite users with long conversations. Mitigation: claude.ai adapter can default to "no skills" and surface them via a connector-side toggle. Operator decision; not blocking.

## 9. Sequencing

Suggested commit-shaped PRs:

1. **S15-a: server `skills` MCP tool (`unitares`).** New handler at `src/mcp_handlers/introspection/skills.py` reading from `unitares/skills/` directory. Frontmatter parser, `triggers` schema, `stale` computation, `registry_version` / `registry_hash`. Single PR to master; auto-merge via `ship.sh` runtime path. **Does not touch any existing skill content.**
2. **S15-b: skill content consolidation.** For each of the 5 differing skills + the 1 plugin-only skill, decide canonical content and update `unitares/skills/`. Tag `last_verified` to current date. One PR, content-only.
3. **S15-c: Claude Code adapter.** Plugin's hook-based skill loader gains an alternate path: fetch from server on session start, fall back to bundled mirror on offline. PR to plugin repo.
4. **S15-d: plugin bundle becomes generated mirror.** `scripts/dev/sync-plugin-skills.sh` populates `unitares-governance-plugin/skills/` from `unitares/skills/`. CI gate added. PR to plugin repo.
5. **S15-e (optional): Hermes adapter.** System-prompt injection of skills matching current-message keywords. Not unitares-server scope — Hermes-side work.
6. **S15-f (optional): claude.ai connector.** Skills as resources surfaced through the Cloudflare-tunneled MCP endpoint. Adapter is built into claude.ai's MCP client; no work this side.

S15-a alone closes the structural source-of-truth problem on the server. S15-b is the content cleanup that makes the surface useful. S15-c through S15-f are the per-client wave.

## 10. What this plan does NOT decide

- **Whether Codex's `commands/*.md` is in scope for S15.** Codex slash commands are a different teaching surface (user-invoked, not model-activated). Recommend treating as a separate concern; S15 is for model-activated skills.
- **Whether the `triggers` schema should match Anthropic's formal Skills spec when it stabilizes.** Currently Skills is in active evolution. S15-a should stay close to existing UNITARES skill frontmatter; align with upstream when it stabilizes.
- **Whether `unitares-dashboard` (plugin-only skill) is canonical or plugin-specific.** Operator-decided in S15-b. Likely canonical (dashboard knowledge is server-knowledge).
- **MCP-spec proposal.** UNITARES ships this as a private tool surface; if Anthropic standardizes a Skills RPC primitive, S15 migrates. Don't pre-commit to a wire format that may be replaced.
- **Multi-language skills.** All current skills are English. S15 schema doesn't preclude `lang: en` fields, but the implementation lands English-only.

## 11. Operator decision points

1. **A / B / C / D?** Recommend A (server-side `skills` MCP tool). Confirm.
2. **Canonical location for skills content.** Recommend `unitares/skills/` (server repo). Plugin becomes mirror. Confirm — alternative is `unitares-governance-plugin/skills/` as canonical with server fetching from there, but server is the more natural authority.
3. **Trigger schema fields.** Recommend three: `keywords`, `tool_calls`, `situations`. Operator may want others (`excluded_tools`, `min_trust_tier`, `requires_resident_class`). Each new field is an adapter-mapping decision.
4. **Freshness threshold default.** Existing `freshness_days: 14` is the default in current SKILL.md files. Keep, or shift to per-skill explicit?
5. **Plugin bundle treatment.** Generated mirror (S15-d) or hand-edited with parity CI? Recommend generated mirror — the parity CI approach failed the equivalent test for `AGENTS.md` / `CLAUDE.md` until it became enforced (and only for byte-identical, not semantic, parity).
6. **Hermes adapter scope.** Out of unitares-server scope. Confirm Hermes adapter is the operator's responsibility (or a Hermes-side contributor's), not this PR's.
7. **claude.ai adapter scope.** Skills surfaced via the MCP endpoint; rendering depends on claude.ai's MCP client behavior. No code on this side.
8. **Canonical-on-server is a one-way decision once any adapter consumes it.** Same forcing-function shape as the s1 council's "A→A′ muscle-memory foreclosure of C" finding. Reversing the canonical location later (e.g., "let's split skills into a separate `unitares-skills` repo for clarity") requires deprecating the MCP tool, not just moving files. Operator should ratify canonical-on-server with this irreversibility explicit, not as an implicit default.
9. **Pair S15-a ship with at least drafted S15-b.** S1's analog was "TTL-shrink paired with secret rotation" — the structural change shipped alongside an operator hygiene step that resolved the known-drifted state at the same SHA. For S15: do not ship the new MCP tool pointing at a `unitares/skills/` directory that is known-divergent from the plugin bundle. Either reconcile content first (one PR, then S15-a) or open both PRs in the same review cycle. Operator confirms sequencing.
10. **Tool-description embedding (§6 complementary cure).** Recommend shipping a small `tool_descriptions.json` v2-ontology audit alongside or before S15-a. This is the cheapest path to closing the immediate Hermes-class incident. Operator decides: before, after, or alongside S15-a — but flag it so it does not slip indefinitely.

## 12. Pointers for next process-instance executing this plan

- Read this doc + `unitares/docs/ontology/identity.md` (target ontology to teach) + `unitares/docs/ontology/s8a-tag-discipline-audit.md` (write-path-drift parable) + S1 doc for retirement-pattern shape.
- Check `plan.md` S15 row for `WIP-PR:` field before opening S15-a.
- Target: `unitares` master. Docs-only preflight (this doc) lands first; S15-a is the first code PR.
- Cite this doc's §4 and §5 in the S15-a PR body. §5 in particular surfaces the activation-trigger portability problem that future adapter PRs must address.
- Verify S15-a does NOT regress `describe_tool` / `list_tools` behavior — separate concern, separate handler.

## 13. Post-S15-a / §6 empirical update (2026-04-25)

**Status of shipped work as of this update:** S15-a (PR #157, server-side `skills` MCP tool), §6 cure (commit `910c0edb`, v2-ontology anti-pattern guards in `tool_descriptions.json`), and S15-b (canonical content consolidation in `unitares/skills/`) all merged to master between this doc's first draft (commit `1d976b1f`) and now.

**Empirical evidence on §9 step 5 (S15-e Hermes adapter) scope.** Test executed 2026-04-25 evening against a Hermes v0.11.0 install on the primary Mac, model `qwen3.6:27b-coding-nvfp4` via local Ollama, MCP server at `http://localhost:8767/mcp/`. Result: model auto-discovered all 24 UNITARES tools and successfully invoked `mcp_unitares_health_check` from a one-shot prompt — without any Hermes-side skill bundle, without S15-c/d/e adapter work, and without any UNITARES-authored skill content reaching Hermes. The §6 tool-description guards alone carried the model. KG: `2026-04-25T13:21:32.782174`.

**What this narrows.** §1d framed the originating Hermes incident as motivating S15-e ("Hermes adapter is needed so the model has access to UNITARES skill content"). The §6 cure displaces that framing. With v2-ontology invariants embedded in tool descriptions, S15-e is no longer the safety net for Hermes-class teaching gaps — it becomes a *recall-speed and procedural-knowledge* optimization (e.g., teaching the model "when to invoke dialectic," not "what `force_new=true` means"). The originating incident closes at §6, not at S15-e.

**Operational implication.** S15-e remains optional and lower-priority than originally framed. The natural ordering is now: §6 audit completeness (more invariants embedded as Hermes-class clients exercise the surface and reveal gaps) before any S15-e adapter design starts. Any future "Hermes is reasoning incorrectly about X" finding should first ask whether X belongs in `tool_descriptions.json`.

**No edits to §1–§12.** This section is a post-ship addendum; the original framing stands as a historical record of what the design assumed before empirical validation.

---

## Appendix: Design echoes from existing UNITARES patterns

This doc reuses three patterns the operator has already validated:

- **`governance_core` fold-back (2026-04-24)** — single source of truth in the server repo, consumers downstream. Same logic applied to skills.
- **`s8a-tag-discipline-audit.md` lesson** — write-path drift can't be fixed by sync discipline; the structural fix is making one path canonical and removing the others.
- **S1 retirement plan structure** — multi-role inventory, options table, sequenced PRs, operator decision points, what-this-doesn't-do scope guard. This doc copies the shape.

---

## Appendix: Call-site quick reference

**New code (S15-a):**
- `src/mcp_handlers/introspection/skills.py` — handler, frontmatter parser, stale computation, registry-hash deterministic ordering
- `src/mcp_handlers/schemas/skills.py` — Pydantic schema for response shape. `version` field typed `Optional[str]` (ISO date string) to match repo convention for nullable string fields. No collision with existing schema field names verified.
- `tests/test_skills_introspection.py` — frontmatter parsing, stale flag, since_version filtering, registry hash stability. Filename follows the `test_<subject>.py` repo convention (not `test_skills_handler.py` — that breaks pattern; existing siblings: `test_identity_session.py`, `test_tool_stability.py`).

**Files touched but not handler-logic (mechanical):**
- `src/mcp_handlers/__init__.py` — one new import line for `skills.py` so the `@mcp_tool` decorator gets picked up by `get_decorator_registry()`. Without this, the new tool is never registered.
- `src/mcp_handlers/middleware/rate_limit_step.py` L53 — add `'skills'` to the `read_only_tools` set. Required for rate-limit parity with `list_tools`; the `rate_limit_exempt=True` decorator metadata is stored in `ToolDefinition` but not consulted by this middleware.

**Existing code unchanged:**
- `src/mcp_handlers/introspection/tool_introspection.py` — `describe_tool` (`handle_describe_tool` at L1010) / `list_tools` (`handle_list_tools` at L52) untouched
- `src/tool_descriptions.json` — untouched in S15-a (separate optional companion PR per §6 audits this for v2-ontology embedding)
- `src/mcp_handlers/introspection/export.py` — untouched. (Note: contains `get_system_history` / `export_to_file` — governance-history export, not introspection. Co-location in `introspection/` directory is accidental; do not model S15-a on it.)
- `unitares/skills/*` — content unchanged in S15-a (S15-b consolidation is the content pass)

**Test surfaces:**
- New: skills-handler-frontmatter-parse (N source_files entries), skills-handler-stale-detection (git-log against source_files), skills-handler-since-version-filter, skills-registry-hash-deterministic, skills-cache-key-identity-blindness (regression test for §4.5 ontology guard).
- Existing: no regression expected; `tool_introspection.py` tests should pass unchanged. Verify `list_tools` response now includes `skills` after the `__init__.py` import lands.
