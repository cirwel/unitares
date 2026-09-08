# UNITARES public interface contract

**Current contract:** `unitares.interface-contract.v1`, version `1.3.0`

UNITARES is MCP-native, but the integration boundary is a set of capabilities,
not one transport. For a selected tool mode, the server advertises the same
callable names and input schemas through:

- Streamable HTTP MCP at `/mcp/`
- REST discovery at `GET /v1/tools`
- local stdio discovery

The checked-in [`interface-contract.v1.json`](interface-contract.v1.json) is the
machine-readable `lite` contract. A live client negotiates the same contract by
calling `list_tools(lite=true)` and reading `interface_contract`; no repository
tag lookup or private server import is required. Its `surface_sha256` changes
whenever the ordered capability records change. CI compares that artifact with
the live registries, so transport drift or an unversioned surface change fails
visibly.

## What v1 guarantees

Each capability record gives one public name, its canonical implementation,
its kind, and its name on each transport. A `workflow_alias` is a
first-class public spelling such as `start_session` or `sync_state`; a
`canonical_tool` is the underlying registered tool. Both are dispatched by the
same server authority.

For the declared mode, a listed capability is:

1. advertised by each local public discovery surface;
2. accepted by the common dispatcher; and
3. described by the same source input schema before transport-specific
   serialization.

The `federation.lifecycle` section also names the product-facing lifecycle
capabilities and their normalized success/failure envelope. It guarantees the
small stable seam a separate userland needs: successful lifecycle calls carry
`success`, `tool`, and `next_action`; optional state, risk, recovery, memory,
and raw-governance fields may be omitted when they have nothing to say. It does
not freeze every diagnostic field returned by Core.

The contract deliberately does **not** imply that a
client host installs lifecycle hooks, forwards edit or stop events, schedules
check-ins, or honors a returned policy action outside UNITARES-governed writes.
Those are host-integration capabilities, documented separately in the
[client capability matrix](integration/CLIENT_CAPABILITY_MATRIX.md).

## Modes and compatibility

`minimal`, `standard`, `lite`, and `full` are server-selected discovery
profiles. They decide what `tools/list` advertises, not what dispatches: every
registered name and every workflow alias is callable by name in every profile
on every transport. `standard` is the server default and advertises eleven names:
the checkpoint loop (`start_session`, `identity`, `sync_state`,
`record_result`, `check_working_state`) plus `search_shared_memory`,
`store_finding`, `update_finding`, `request_review`, `consult`, and
`self_recovery`. `minimal`
advertises the checkpoint loop alone. The checked-in artifact uses `lite`, the
wider agent-facing profile; full mode adds administrative and specialist
tools. The live handshake (`list_tools(lite=true)`) reports the profile the
server runs, so a `minimal` deployment answers with five capabilities and its
own surface hash while still dispatching the lite names.

Because a schema-driven client offers the model only the names discovery
returned, the profile is a capability boundary for such clients even though it
is not one for dispatch. The server therefore states its profile, and what it
is withholding, in the MCP `instructions` string returned at connect. That
string is orientation, not contract: it is not part of the surface hash and
may be reworded in any release.

Adding a compatible capability increments the contract version. Renaming,
removing, or changing the meaning of an existing capability requires a new
major schema contract or an explicit deprecation window. Raw implementation
names may remain callable for compatibility even when the preferred workflow
alias is the documented integration name.

The two identifiers serve different jobs:

- `unitares.interface-contract.v1` is the schema family. Its `v1` changes only
  for a breaking change to the contract document's shape.
- `version: 1.3.0` is the negotiated interface release. Compatible additions
  advance it without forcing clients to learn a new schema family (1.2.0,
  2026-09-07: `observe` and `describe_tool` declare parameters their handlers
  already read; 1.3.0, 2026-09-08: `describe_tool` takes `action` and answers
  for one action of a consolidated router, and `dialectic` drops the `vote`
  parameter, which named an action the router does not route and which no
  handler read).

A consolidated router advertises the union of every action's parameters,
because the wire schema must be flat: the MCP wrapper builds a tool's argument
model from top-level properties, so a per-action `oneOf` would not survive
registration. That union is the contract. Which of those parameters each
action uses is declared alongside them, as `ACTION_FIELDS` on the router's
parameter model, and `describe_tool(tool_name=..., action=...)` serves it. The
narrowed schema is a description of one call, not a second contract: the wire
still accepts and ignores the parameters of other actions.

Core currently supports `mcp>=1.26.0,<3.0.0`. Both admitted major versions are
tested, and the newest in-range resolution is a blocking CI lane. A client
should negotiate the UNITARES contract above rather than infer compatibility
from its locally installed MCP package version.

Regenerate the artifact from the repository root with:

```bash
python3 -m src.interface_contract > docs/interface-contract.v1.json
```

Then run `tests/test_interface_contract.py` and the normal repository test gate.
