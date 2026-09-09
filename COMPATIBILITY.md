# Compatibility and Naming

UNITARES components use independent version series because they are separate
artifacts. A higher number in one series does not imply a newer or stronger
artifact in another.

## Component versions and publication

| Artifact | Current version | Role and compatibility |
|---|---:|---|
| UNITARES server | `v2.22.0` | Source version string. Master also contains unreleased API and skills changes beyond this tag; see the Unreleased changelog. Source delivery alone does not establish artifact availability. |
| Published server/container | `v2.21.0` | Verified installation baseline retained while the v2.22.0 Compose correction is prepared. v2.22.0 is published but does not contain #2092; see the [errata](docs/releases/2.22.0-errata.md). Advance install pins only after the corrected tag, release page, and container are verified. |
| `unitares-governance` plugin | `v0.4.17` | Historical bundle evidence: all seven skill files and `SKILLS_MANIFEST.sha256` match server tag `v2.22.0` (`b1360809`). Current master has since changed the skills in #2095 and #2097; this is not current-tree parity. This establishes bundle parity at that tag, not a new end-to-end host test. The previously recorded host baseline is Claude Code 2.1.220+ and Codex CLI 0.146.0+. |
| `unitares-sdk` | `0.3.0` | Published Python client for resident and custom integrations. This is the first release that completes the documented fresh-resident onboarding flow against the current identity contract. It also preserves server identity guidance as typed errors, refuses an unregistered persistent mint rather than running an unprotected resident, stops templated cycle and heartbeat check-ins from inventing agent confidence or authorship, accepts optional MCP bearer credentials, and carries identity-bound lease/effect helpers. This is a behavioral minor release: `GovernanceAgent` subclasses should review the new confidence, epistemic-class, and resident-registration defaults. Install it with `pip install unitares-sdk==0.3.0`; use a server Git tag only when deliberately testing an unreleased SDK build. |
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

- v2.23.0 preserves registered callable names, input schemas, and lifecycle
  envelopes; no database migration is introduced. Discovery profiles are gone
  (#2137): every transport advertises one complete catalog — every registered
  tool plus the primary workflow aliases — so a client that selected tools from
  discovery on v2.22.0 sees additional names and no removals. Legacy
  `GOVERNANCE_TOOL_MODE` settings and REST `mode` query parameters are accepted
  but ignored, `minimal` included, so that value no longer restores the v2.22.0
  default surface; see the
  [interface contract](docs/INTERFACE_CONTRACT.md). The server also returns an
  MCP `instructions` string describing the catalog.
- v2.22.0 preserves registered callable names, input schemas, lifecycle
  envelopes, and the selectable `lite` contract; no database migration is
  introduced. Its default discovery profile changes from 29 tools to five.
  Clients that select tools from discovery need `GOVERNANCE_TOOL_MODE=lite`
  in the server environment to retain the wider workflows. Recreate the
  Compose service and reconnect clients after changing the setting. This is
  a discovery behavior change, not a promise of unchanged default workflows.
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
