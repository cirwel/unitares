# Tool-surface legibility v0 — what an agent sees at selection time

Status: Draft v0, design-only, unreviewed
Date: 2026-08-29

## Problem

An agent choosing a tool reads names and first lines, under time pressure, while
already mid-task. The current advertised (lite) surface fails that reader in
three ways, none of which is a missing capability:

1. **Near-synonym clusters with no selection cue.** Five advertised tools mean
   "ask another model" (`call_model`, `delegate_inference`, `consult`,
   `list_inference_hosts`, `describe_inference_host`); in plain English *call*,
   *delegate*, and *consult* are one verb. Four advertised tools write to shared
   memory (`store_finding`, `update_finding`, `leave_note`,
   `knowledge`). The names alone cannot route a first-contact agent.
2. **Descriptions are written for the ontology, not the decision.** From
   `src/tool_descriptions.py` + `tool_descriptions.json` as of this writing:
   `process_agent_update` 4,641 chars, `onboard` 4,482, `identity` 3,684,
   `search_knowledge_graph` 3,406, `call_model` 2,703. The `onboard` text opens
   with framing rather than when-to-use, then carries dated policy IDs (S1-c
   2026-05-23, Identity Honesty Part C 2026-04-18) and ANTI-PATTERN advisories
   addressed to *client/harness authors* — all inside the payload an agent
   skims while deciding what to call. That history is load-bearing; its
   location is the defect.
3. **Nothing orients.** The surface is only discoverable by reading 25+
   descriptions. A Claude Code cloud session on 2026-08-29 ran with the full
   lite surface attached, shipped two PRs, and made zero governance tool calls
   — state 1 of the measurement-authority taxonomy ("never surfaced": nothing
   in the injected context routed toward the tools), observed directly rather
   than inferred from a count.

## What is already done (this proposal builds on it, not past it)

- **Feb 2026 consolidation**: `knowledge`, `observe`, `dialectic`, `agent`,
  `calibration`, `config`, `export` already route sub-operations through
  `action=` parameters.
- **Friendly-verb promotion + #1994** (2026-08-29): the friendly workflow verbs
  (`start_session`, `sync_state`, `check_working_state`,
  `search_shared_memory`, `record_result`, `request_review`) carry the wire;
  raw twins stay registered but are no longer advertised (except `onboard`,
  deliberately, pending a cross-repo follow-up). Duplicate registrations
  retired; `tests/test_lite_wire_surface.py` pins the advertised set.
- **`consult-advisory-facade-v1`** (2026-08-24): the advisory/governed split is
  decided — `consult` returns advisory evidence and creates no record;
  `request_review` is governed, on-record judgment.
- **Audit tooling**: `scripts/dev/tool_edge_index.py` /
  `docs/dev/TOOL_EDGE_INDEX.md` resolve every registered name to its handler.

The remaining gap is *presentation*: what the advertised set says in its first
line, and what greets an agent at session start.

## Proposal

### 1. A description contract, lintable

Every advertised tool description conforms to:

- **Line 1 — the routing line.** ≤ 140 characters, starts with a verb, states
  when to use this tool in task vocabulary, and for tools inside a cluster,
  names the discriminator against its nearest neighbor ("…; for X use `<other>`").
- **Block 2 — behavior-changing parameters/actions.** What `action=` values
  exist, what changes the response shape.
- **Everything else moves.** Policy history, dated decisions, anti-pattern
  advisories, notes to harness authors: out of the wire payload, into the
  `describe_tool(name)` detail payload and `docs/`. `tool_descriptions.json`
  splits each entry into `summary` (advertised) and `detail` (served by
  `describe_tool`); `list_tools` and the MCP tool listing advertise `summary`
  only.

Enforcement is a deterministic checker (`scripts/dev/check_tool_descriptions.py`
or a rule folded into the existing surface audit): first-line length, verb
start, and no dated policy-ID pattern in `summary`. Free, offline, CI-safe —
consistent with the execution-cost policy.

### 2. Routing lines for the ambiguous clusters

The two clusters that need discriminators, written against the
already-decided facade semantics:

| Tool | Proposed routing line |
|---|---|
| `consult` | Ask a model for advisory help — evidence only, creates no governance record; for on-record judgment use `request_review`. |
| `request_review` | Request governed, on-record review with reviewer provenance; for off-record advice use `consult`. |
| `delegate_inference` | Hand a bounded task to a configured strong-model host and get the result back; for a raw completion use `call_model`. |
| `call_model` | Run a raw completion on a configured inference host (plumbing); for advisory help prefer `consult`. |
| `list_inference_hosts` | List the inference hosts this deployment has configured, with capability tags. |
| `describe_inference_host` | Inspect one configured inference host (models, limits, health). |
| `store_finding` | Write a durable finding to shared memory — search first; for revising an existing entry use `update_finding`. |
| `update_finding` | Revise or supersede a finding already in shared memory, preserving the correction chain. |
| `search_shared_memory` | Search shared memory before writing — the read half of the search-before-write discipline. |
| `leave_note` | Leave a quick lightweight note for other agents or the operator; for durable findings use `store_finding`. |
| `knowledge` | Full knowledge-graph interface (`action=` store/search/get/list/update/note/cleanup/stats) behind the friendly verbs above. |

The remaining advertised tools get the same treatment mechanically; these
eleven are the ones where the *content* of the line is a design decision.
(`leave_note`'s exact wording should follow whatever #2006 lands.)

### 3. Orientation at session start

A ten-line capability map injected once at session start is the one surfacing
mechanism that reliably lands — a zero measures what was injected. Shape:

> You have governance tools: `start_session` once, `sync_state` as you work,
> `check_working_state` to read your own state, `search_shared_memory` /
> `store_finding` for shared memory, `request_review` when a decision needs
> a second reader, `consult` for advisory help, `self_recovery` if stuck.
> `list_tools` shows the rest.

The injection point is the gov-plugin's `hooks/session-start` (the plugin owns
the hook lifecycle), so the text ships here as a server-side constant or doc
the plugin renders — a cross-repo follow-up, sequenced after the routing lines
exist so the map and the wire agree.

## Non-goals

- **No removals.** Nothing here retires a capability, and no future usage
  count harvested from this surface may — measurement authority applies. The
  checker retires bad *descriptions*, which are instruments.
- **No identity-surface edits in passing.** `onboard` / `identity` /
  `start_session` descriptions sit inside the writer-locked identity surface;
  their `summary`/`detail` split lands as one coordinated change on that
  surface, not as drive-by edits from this thread.
- **No new tools, no renames.** The friendly-verb namespace decided by the
  promotion + #1994 stands; this proposal only changes what the existing names
  say and how the deep documentation is reached.

## Decision points for the operator

1. Adopt the description contract (§1) as the standard for advertised tools?
2. Approve the eleven routing lines (§2) as drafted, or edit in place?
3. Sequence: routing lines + `summary`/`detail` split first, then the plugin
   orientation map — or orientation first with today's descriptions?
4. Should `describe_inference_host` stay individually advertised in lite, or
   fold under `list_inference_hosts` detail? (Flagged only — the
   inference-delegation capability-registry thread owns host-surface shape.)
