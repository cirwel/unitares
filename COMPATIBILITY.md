# Compatibility and Naming

UNITARES components use independent version series because they are separate
artifacts. A higher number in one series does not imply a newer or stronger
artifact in another.

## Current public set

| Artifact | Current version | Role and compatibility |
|---|---:|---|
| UNITARES server | `v2.18.0` | Current supported runtime and canonical API behavior. |
| `unitares-governance` plugin | `v0.4.13` | Claude Code/Codex package release compatible with server `v2.18.0`; its tagged skill bundle predates the post-2.17 coherence-semantics synchronization merged in plugin PR #116. Use a later tagged plugin release for semantic alignment once published. Tested with Claude Code 2.1.220+ and Codex CLI 0.146.0+. |
| `unitares-sdk` | `0.1.0` | In-tree Python client tested against the server at the same commit. Until its first PyPI release, install from the server's matching Git tag. |
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
