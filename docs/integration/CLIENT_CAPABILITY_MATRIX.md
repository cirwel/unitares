# Client capability matrix

MCP access and lifecycle integration solve different problems. MCP makes Core
capabilities reachable. A host plugin, adapter, or agent runtime decides when
to call them and what to do with the answer.

| Client shape | Discover and call Core tools | Automatic process onboarding | Automatic check-in cadence | Host event capture | Acts on returned policy |
|---|---:|---:|---:|---:|---:|
| Direct MCP client | Yes | No | No | No | Client-owned |
| Codex governance plugin | Yes | Host lifecycle binding | Sparse, meaningful-boundary check-ins | Plugin-visible events only | Codex/host-owned outside governed writes |
| Claude Code governance plugin | Yes | Host lifecycle binding | Denser event-driven check-ins | Hook-visible events only | Claude/host-owned outside governed writes |
| Hermes host adapter | Yes | Adapter-owned | Adapter-owned | Adapter-visible events only | Hermes/adapter-owned outside governed writes |
| UNITARES Resident (early skeleton) | Must use the same public contract | Runtime-owned | Runtime-owned | Runtime-owned | Runtime-owned outside governed writes |

“Automatic” is not a property of MCP itself. It comes from the client host's
lifecycle and hook model. That explains the observable difference between
Codex and Claude integrations: Codex emphasizes a strong explicit start and
sparser semantic checkpoints, while Claude Code exposes a denser hook stream
that can produce more frequent governance events. Core accepts both patterns;
it does not require their event volumes to match.

UNITARES Resident is therefore a sibling userland, not an elevated mode of
Core. Its early skeleton establishes that boundary; the usable product may
provide a cohesive Hermes-like experience—persistent
conversation, providers, tools, scheduling, and queues—but it must connect
through MCP or the public SDK, hold an ordinary agent identity, and receive the
same policy responses as external clients. No direct database, scoring, or
private dispatcher path is part of that product.

See the versioned [public interface contract](../INTERFACE_CONTRACT.md) for the
transport-neutral capability seam and [`MCP_CLIENTS.md`](MCP_CLIENTS.md) for
connection details.
