# UNITARES public interface contract

**Current contract:** `unitares.interface-contract.v1`, version `1.1.0`

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

`minimal`, `lite`, and `full` are server-selected discovery profiles. The
checked-in artifact uses `lite`, the default agent-facing profile. Full mode
adds administrative and specialist tools; minimal mode keeps only bootstrap
and introspection capabilities.

Adding a compatible capability increments the contract version. Renaming,
removing, or changing the meaning of an existing capability requires a new
major schema contract or an explicit deprecation window. Raw implementation
names may remain callable for compatibility even when the preferred workflow
alias is the documented integration name.

The two identifiers serve different jobs:

- `unitares.interface-contract.v1` is the schema family. Its `v1` changes only
  for a breaking change to the contract document's shape.
- `version: 1.1.0` is the negotiated interface release. Compatible additions
  advance it without forcing clients to learn a new schema family.

Core currently supports `mcp>=1.26.0,<3.0.0`. Both admitted major versions are
tested, and the newest in-range resolution is a blocking CI lane. A client
should negotiate the UNITARES contract above rather than infer compatibility
from its locally installed MCP package version.

Regenerate the artifact from the repository root with:

```bash
python3 -m src.interface_contract > docs/interface-contract.v1.json
```

Then run `tests/test_interface_contract.py` and the normal repository test gate.
