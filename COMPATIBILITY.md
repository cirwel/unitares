# Compatibility and Naming

UNITARES components use independent version series because they are separate
artifacts. A higher number in one series does not imply a newer or stronger
artifact in another.

## Current public set

| Artifact | Current version | Role and compatibility |
|---|---:|---|
| UNITARES server | `v2.20.0` | Current supported runtime and canonical API behavior. |
| `unitares-governance` plugin | `v0.4.16` | Claude Code/Codex package release aligned with server `v2.20.0`. Its tagged skill bundle is a byte-identical mirror of this repository's `skills/` at the v2.20.0 release, verified against the published `SKILLS_MANIFEST.sha256`, and carries the coherence-semantics correction from plugin PR #116 and the v2.19.0 margin-semantics correction. Tested with Claude Code 2.1.220+ and Codex CLI 0.146.0+. |
| `unitares-sdk` | `0.2.2` | Published Python client for resident and custom integrations. `0.2.2` completes the migration `0.2.1` left open: HTTP-client injection (including the UDS substrate-attestation transport) now resolves through the same `mcp_httpx()` seam the server uses, so the client builds an `httpx2`-compatible client under `mcp` 2.x instead of silently losing the server-push stream; the `mcp` requirement widens to `<3.0.0` accordingly. Also fixes a latent bug where the SDK's retryable-connection-error check stopped matching once the injected client was no longer `httpx`. Behavior is otherwise unchanged, so `0.1.0`/`0.2.1` callers keep working. Install the public release with `pip install unitares-sdk==0.2.2`; use the server's matching Git tag only when deliberately testing unreleased SDK changes. |
| `unitares-host-adapter` | `0.2` alpha | Separately released host bindings; capabilities vary by host and remain pre-stable. |
| Paper / reproducibility kit | paper `v6.9.1`, kit `v6.8.1-repro` | Research and evaluation artifacts, not runtime dependencies or server compatibility numbers. |

The plugin row above was corrected after the 2.18.0 release; the release-time
table claimed a stronger alignment than the tagged bundle carried. See
[docs/releases/2.18.0-errata.md](docs/releases/2.18.0-errata.md) for what was
stated, what is actually true, and the evidence a future release note needs.

## Names you will encounter

| Name | Meaning |
|---|---|
| UNITARES | Project and server product name. |
| `governance-mcp` | Historical Python distribution name for the server checkout; it is not currently published on PyPI. |
| `unitares-sdk` / `unitares_sdk` | Python distribution and import package for client and resident integrations. |
| `unitares-governance` | Installable Claude Code/Codex plugin name. |

Changing the historical server distribution name would break existing editable
installs and automation for little immediate user value. New public prose should
lead with **UNITARES server** and treat `governance-mcp` as package metadata.

## Compatibility policy

- The latest server release and `master` are the supported server lines; see
  [SECURITY.md](SECURITY.md).
- Server aliases and the documented response envelope are the preferred client
  contract. Experimental or operator-internal fields are outside that contract
  and may change in a documented release.
- The in-tree SDK is tested in server CI. A tagged SDK release must pass its
  standalone package tests before publication.
- The governance plugin and host adapter remain thin clients: server policy,
  scoring, identity semantics, and storage stay in this repository.
- When reporting an integration problem, include all component versions rather
  than only the server version.

See the [release process](docs/operations/RELEASE_PROCESS.md) for the checks that
keep this table and published artifacts aligned.
