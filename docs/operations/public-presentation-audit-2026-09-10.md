# Public presentation audit — 2026-09-10

Scope: README, product definition, package/MCP metadata, discovery contract,
and repository-owned Glama artifacts. Documentation and descriptive metadata
only; no tool registration, routing, schema/default, tier, authorization,
storage, deployment command, or inference behavior changes.

## Evidence and findings

- Source baseline: `d97a958c` on `origin/master`. README led with infrastructure
  and a same-agent question; product definition led with record/score/interrupt/
  remember. Neither gave reconstruction an explicit place in the public workflow.
- `pyproject.toml` described runtime telemetry and policy feedback;
  `src/tool_modes.py:build_server_instructions` led with behavioral estimation.
  Both underspecified the shared identity/evidence/review record.
- `src/tool_modes.py`, `src/tool_meta.py`, and `docs/INTERFACE_CONTRACT.md`
  establish one complete catalog, with separate browsing tiers and action
  authorization. A core-versus-advanced explanation must not imply mode filters.
- [Glama overview](https://glama.ai/mcp/servers/cirwel/unitares), read on the
  audit date, combined copied README material with platform-written summaries.
  Its identity summary incorrectly described restarted processes as the same
  accountable agent. Its copied README also made a blanket data-locality claim.
- The prior conversation's 14-versus-52 report is historical context, not a
  verified current count or proof that a supported 14-tool profile exists.
  `docs/deployment/glama.md` records a concrete September 8 missing-dependency
  reproduction and a replacement storage-backed bundle. This audit did not
  reproduce the live Glama deployment or infer its current build from a count.
- `glama.json` contains schema/maintainer ownership metadata only. The actual
  installation inputs are `Dockerfile.glama`, `docker/glama/build-spec.json`,
  and `docker/glama/environment-schema.json`. The replacement build spec has
  `pinnedCommit: null`; the deployment operator must pin a reviewed commit.
- Open PR #2158 edits tool/alias descriptions and skill artifacts. This patch
  avoids those files; alias repair is separate from the product framing here.

## Concrete change plan and patch coverage

| Change | Artifact | Acceptance criterion |
|---|---|---|
| State the federation kernel and its limits | README, product definition | Identity, claims/evidence, review, outcomes, reconstruction appear together; no claim of cross-server replication or proved comparative benefit. |
| Map the concept onto existing operations | Capability/deployment guide | Real tool names, record limitations, reviewer/inference prerequisites, and client-assembled reconstruction are explicit. |
| Align client-facing introduction | Package description, MCP initialize instructions | Same product framing; fresh-process identity and complete catalog preserved. Only descriptive text changes in Python. |
| Separate discovery from readiness | Interface contract prose, capability guide | Core is a reading path; tiers, ordered capabilities, input schemas and hashes remain unchanged. |
| Clarify installation and external processing | README, capability guide, Glama guide | Compose, private bundle, and existing-server client profiles distinguished; incomplete storage setup is not sold as lightweight core. |
| Prepare directory correction | Glama guide | Replacement summary and verification checklist available without publishing or changing account configuration. |

## Verification and follow-through

Before commit: inspect the diff, validate local Markdown targets and JSON/TOML,
verify Python differs only in the instructions string, and run the repository
required test-cache gate plus discovery/instructions checks. Preserve the
machine-readable interface artifact and Glama build/environment values.

After review and merge: the operator can apply the Glama copy correction, pin
the reviewed build commit, validate persistent storage and representative calls,
then refresh/release the listing. A repository merge alone does not establish
that the directory regenerated its copy or deployed the new bundle. No Glama
account mutation, paid provisioning, or publication is part of this patch.

Remaining independent work: evaluate reconstruction against Git plus structured
handoff; verify deployed catalog parity with recorded transport/build context;
validate optional inference and completed reviewer flows in the target
installation. Those are not evidence supplied by this documentation change.

## Local validation receipt

- `./scripts/dev/test-cache.sh --quick`: 15,620 passed, 35 skipped in 500.96s
  using system Python 3.14. The first attempt selected a repository virtual
  environment without pytest; it did not execute tests.
- Focused tool-mode/listing tests: 51 passed.
- Python AST comparison: no change outside the MCP instructions string.
- JSON/TOML parsing and edited-document local link checks passed; interface and
  Glama configuration artifacts are byte-identical to the source baseline.
- Generated flags, ports, and tool-edge references are fresh; diff whitespace
  check passed. No container or live Glama validation was rerun for text edits.
