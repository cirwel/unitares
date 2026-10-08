# Compatibility and Naming

UNITARES components use independent version series because they are separate
artifacts. A higher number in one series does not imply a newer or stronger
artifact in another.

## Component versions and publication

| Artifact | Current version | Role and compatibility |
|---|---:|---|
| UNITARES server | `v3.3.0` | Source version string. Master also contains unreleased API and skills changes beyond this tag; see the Unreleased changelog. Source delivery alone does not establish artifact availability. |
| Published server/container | `v3.2.0` | Verified release. The tag, its published release page, the server and lease-plane images for linux/amd64 and linux/arm64, their SPDX SBOMs, and provenance bound to the tag's source commit were verified before GHCR `latest` moved to this release in [Promote Release run 37445369555](https://github.com/cirwel/unitares/actions/runs/37445369555). What changed and how to upgrade: [release notes](https://github.com/cirwel/unitares/releases/tag/v3.2.0). |
| `unitares-governance` plugin | `v0.4.19` | Carries the skills bundle for server `v3.1.0`: its `skills/` mirrors master at `ef127104`, compared file by file at release. This is bundle parity, not a new end-to-end host test. The previously recorded host baseline is Claude Code 2.1.220+ and Codex CLI 0.146.0+. [Release notes](https://github.com/cirwel/unitares-governance-plugin/releases/tag/v0.4.19). |
| `unitares-sdk` | `0.4.0` | Published Python client for resident and custom integrations, released with server v3.0.0. A behavioral minor release: `checkin` reads the verdict from the response envelope, so `GovernanceAgent`'s pause and reject handling (`VerdictError`) can now fire where every verdict used to parse as proceed; `get_metrics().action` is the policy action; `audit_knowledge` no longer requests a model by default; and the async `GovernanceClient` raises `GovernanceToolRefused`, a subclass of `GovernanceConnectionError`, when a tool answers `success: false`. It adds an optional NeMo Relay integration (`unitares-sdk[nemo-relay]`). Install it with `pip install unitares-sdk==0.4.0`; use a server Git tag only when deliberately testing an unreleased SDK build. |
| `unitares-host-adapter` | `0.3` alpha | Separately released host bindings; capabilities vary by host and remain pre-stable. |
| Paper / reproducibility kit | paper `v6.9.3`, kit `v6.8.1-repro` | Research and evaluation artifacts, not runtime dependencies or server compatibility numbers. |

The plugin row above was corrected after the 2.18.0 release; the release-time
table claimed a stronger alignment than the tagged bundle carried. See
[docs/releases/2.18.0-errata.md](releases/2.18.0-errata.md) for what was
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

- v3.3.0 removes no registered callable's canonical name and no input
  parameter. It changes identity defaults in ways an existing install can
  notice; the release is a minor by operator decision (2026-10-08), because
  each change has a setting that restores the old behavior or a one-time
  step, and a client that mints with `start_session(force_new=true)` and
  passes back the returned `client_session_id` (the SDK, the governance
  plugin, the Hermes host adapter, the reference residents) needs neither.
  Strict identity is the default: a bare UUID resume without a continuity
  token, an unresolved write, and an arg-less `onboard()` are refused, and
  `STRICT_IDENTITY_REQUIRED=false` with `UNITARES_IDENTITY_STRICT=log`
  restores the permissive behavior (#2666). Credentials go only to a caller
  that proved ownership; `UNITARES_CREDENTIAL_ISSUANCE=log` issues as before
  and logs what it would withhold (#2681). Stable session ids are keyed
  (`agent-{uuid12}-{tag}`) and a legacy `agent-{uuid12}` id is refused;
  `UNITARES_LEGACY_SESSION_IDS=log` or `accept` admits it during a migration
  (#2702). The continuity key is the server's own: an install that left
  `UNITARES_CONTINUITY_TOKEN_SECRET` unset, blank or at the Compose default
  loses its outstanding continuity tokens and effect grants once, and each
  resident anchor holding such a token must be moved aside and re-provisioned
  with `scripts/ops/provision_resident_anchor.py --apply` (#2674). Archiving,
  deleting or resuming another agent needs a valid `X-Unitares-Operator`
  token (#2667). The LaunchAgent template and `scripts/ops/start_unitares.sh`
  bind to loopback; clients on other machines need
  `UNITARES_BIND_ALL_INTERFACES=1`, and Docker Compose installs are unchanged
  (#2659). The interface contract moves 1.27.0 → 1.31.0 to record
  authorization and output changes; no parameter is added, removed or
  renamed. One database migration is
  introduced, `db/postgres/migrations/073_retention_keep_record.sql`, which
  stops partition maintenance from deleting check-ins and outcome events and
  dropping old audit months; until it is applied the old retention keeps
  running. Apply it with `scripts/unitares update` on Docker Compose or
  `python3 scripts/dev/apply_migrations.py --apply` on an operator install;
  the code does not depend on it, so rolling back with it applied keeps the
  record and is safe (#2711). v3.3.0 also removes the three older
  local-model setting names that v3.2.0 kept
  as aliases: `UNITARES_OLLAMA_BASE` and `UNITARES_OLLAMA_BASE_URL` (replaced
  by `UNITARES_MODEL_BASE_URL`) and `UNITARES_LLM_MODEL` (replaced by
  `UNITARES_MODEL_ID`). Nothing reads them any more, and Docker Compose no
  longer passes them into the server container. An install that still sets
  only the old names gets no error and no warning: the server, the
  orchestrated reviewer and the local residents fall back to the defaults
  (`http://localhost:11434/v1` and `gemma4:latest`), which inside a container
  usually means consult and the local reviewer cannot reach a model. Rename
  the settings in `.env`, the LaunchAgent plist, or wherever the environment
  is set; an Ollama root URL can move over unchanged, because a base with no
  path gets `/v1` added. `./scripts/unitares model` rewrites `.env` with the
  new names.
- v3.2.0 removes no registered callable's canonical name. It retires 37 of
  the last 38 pre-consolidation aliases, each of which renamed one router call
  and injected its action (`list_agents`, `observe_agent`,
  `request_dialectic_review`, `store_knowledge_graph`, `check_calibration`,
  `get_system_history`, `get_connection_status`, and 30 more; the full list and
  replacements are in the [changelog entry](CHANGELOG.md)); each now returns
  `tool_not_found_error`, and the same router call with that action replaces it
  (#2593). The eight advertised workflow aliases and `get_server_info` remain,
  and the advertised surface digest is unchanged. These names were not
  undocumented: v3.1.0's tool reference listed them in an "Older names"
  column, and they dispatched on REST `/v1/tools/call` and in stdio, though
  not on `/mcp`, and no listing advertised them. The release is a minor by
  operator decision (2026-10-04): the "Older names" column records legacy
  redirects, not part of the client contract, which is the router call and
  action each one named, and that contract is unchanged. A client still
  sending an older name over REST or stdio must switch to the router call. The local model endpoint is
  now set with `UNITARES_MODEL_BASE_URL` and `UNITARES_MODEL_ID`;
  `UNITARES_OLLAMA_BASE`, `UNITARES_OLLAMA_BASE_URL` and `UNITARES_LLM_MODEL`
  keep working as aliases until v3.3.0. A `privacy='local'` model request
  (the default) to an endpoint classified as external is now refused with
  `MODEL_ENDPOINT_NOT_LOCAL`; set `UNITARES_MODEL_LOCAL_HOSTS` or
  `UNITARES_MODEL_PRIVACY` if a local endpoint is misclassified (#2571). The
  interface contract moves 1.25.0 → 1.27.0 with compatible description changes.
  Dependency floors rise to `numpy>=2.3.2` and `starlette>=1.0.0`, and
  `pydantic>=2.12.0` is declared (#2641). On the default local posture, a
  client that reaches the server by a dotted hostname (`curl`, the SDKs, a
  Tailscale MagicDNS name) now needs that host in `UNITARES_MCP_ALLOWED_HOSTS`
  or a bearer or passkey session, and a browser page from another origin no
  longer gets the trusted-network bypass (#2658). No database migration is
  introduced.
- v3.1.0 removes no registered callable's canonical name, but retires 24
  unadvertised aliases that only redirected a guessed or pre-consolidation
  name to one that keeps its own (`status`, `start`, `checkin`, `hello`,
  `get_agent_api_key`, and 20 more; the full list and replacements are in the
  [changelog entry](CHANGELOG.md)); each now returns `tool_not_found_error`
  (#2576). The eight advertised workflow aliases, the complete catalog, and
  the interface contract's advertised surface digest are unchanged. The HTTP
  adjudication surface (`/v1/sentinel/adjudication-queue`, `/adjudicate`,
  `/model-adjudicate`) is removed; it was mounted only by the opt-in
  `reference-residents` route pack, so a default install is unaffected, and
  Sentinel's CLI `--resolve`/`--dismiss` paths remain the supported way to
  record an operator verdict (#2546). Two runtime defaults tighten: the REST
  local-posture auth bypass no longer trusts `100.64.0.0/10` (Tailscale/CGNAT)
  by default, so a deployment reached over a tailnet or another overlay must
  set `UNITARES_TRUSTED_NETWORKS` explicitly or its callers get 401 (#2560);
  and the Compose stack stops publishing Postgres and Redis to the host and
  drops container privileges, so a deployment that reached either directly
  needs the new `docker-compose.admin.yml` overlay (#2580). No database
  migration is introduced.
- v3.0.0 preserves lifecycle envelopes; it does not preserve the registered
  callable names, the `dialectic` wire schema, or the database schema.
  `direct_resume_if_safe`, deprecated 2026-01-29 and advertised only in `full`
  mode, is removed with no alias (#2093); the old name now returns
  `tool_not_found_error`. Repoint callers at `self_recovery(action="quick")`
  when risk is below 0.40 with no void, and at `self_recovery(action="review")`
  with a reflection otherwise; the removed tool resumed without a reflection up
  to 0.60, and retiring that band is the substance of the removal. `dialectic`
  drops the `vote` parameter from its wire schema (#2103); no handler read it,
  and the schema still ignores undeclared keys, so a client that keeps sending
  it is not rejected and needs no change. No other advertised parameter is
  removed or renamed; `cirs_protocol`, `observe`, `describe_tool` and
  `list_tools` gain declarations instead, which tightens validation for REST
  and in-process callers that used to receive those keys raw: a string boolean
  is now parsed, and an explicit null for a parameter that has a default is
  refused. Two database migrations are introduced,
  `db/postgres/migrations/070_outcome_prediction_bindings.sql` and
  `071_knowledge_closure_class_survives_tiering.sql`, and the server never
  applies migrations itself: run
  `python3 scripts/dev/apply_migrations.py --apply` against an existing
  database, or `scripts/install/bootstrap_postgres.sh --apply` on a fresh one,
  before the new code starts. Until 070 lands, an `outcome_event` /
  `record_result` call carrying `prediction_id` fails with `DB_ERROR`; calls
  without one are unaffected. Until 071 lands, a finding closed with a
  `closure_class` cannot be archived or moved to cold: the knowledge-graph
  lifecycle leaves it `resolved` and logs the refusal on every run (on the
  Postgres knowledge backend the run stops at that row), and an update that
  sets `archived` or `cold` on it fails. `scripts/ops/deploy-mcp.sh` refuses
  to restart across either gap and prints the apply recipe. Rolling the code
  back while 071 stays applied is safe only while no finding carries a
  `closure_class`: earlier code does not clear the class when it reopens a
  finding, so reopening a classified one (dialectic's resolution included)
  violates the constraint and is reported as "Discovery not found". Check with
  `SELECT count(*) FROM knowledge.discoveries WHERE closure_class IS NOT NULL`;
  if it is not zero, either roll forward or first run
  `UPDATE knowledge.discoveries SET closure_class = NULL, closure_evidence = NULL WHERE closure_class IS NOT NULL`,
  which discards the classes. On the AGE backend the graph nodes keep their
  class properties; earlier code does not read them, but after a later roll
  forward those findings read as classified again until they are next
  reopened or reclassified. Discovery profiles are gone
  (#2137), and the initial listing is progressive (#2328): `tools/list` starts
  with the workflow tools plus `list_tools`, `describe_tool` and `use_tool`,
  and the rest of the catalog stays callable and reachable through them. A
  client that took its tools from a v2.22.0 `lite` or `full` listing therefore
  sees fewer names in the initial list; set `UNITARES_TOOL_ADVERTISEMENT=full`
  to advertise every schema up front. The only callable removed is
  `direct_resume_if_safe`, which only `full` mode advertised. Legacy
  `GOVERNANCE_TOOL_MODE` settings and REST `mode` query parameters are accepted
  but ignored, `minimal` included, so that value no longer restores the v2.22.0
  default surface; see the
  [interface contract](INTERFACE_CONTRACT.md). The server also returns an
  MCP `instructions` string describing the catalog.
- v2.22.0 preserves registered callable names, input schemas, lifecycle
  envelopes, and the selectable `lite` contract; no database migration is
  introduced. Its default discovery profile changes from 29 tools to five.
  Clients that select tools from discovery need `GOVERNANCE_TOOL_MODE=lite`
  in the server environment to retain the wider workflows. Recreate the
  Compose service and reconnect clients after changing the setting. This is
  a discovery behavior change, not a promise of unchanged default workflows.
- The latest server release and `master` are the supported server lines; see
  [SECURITY.md](../.github/SECURITY.md).
- Server aliases and the documented response envelope are the preferred client
  contract. Experimental or operator-internal fields are outside that contract
  and may change in a documented release.
- The in-tree SDK is tested in server CI. A tagged SDK release must pass its
  standalone package tests before publication.
- The governance plugin and host adapter remain thin clients: server policy,
  scoring, identity semantics, and storage stay in this repository.
- When reporting an integration problem, include all component versions rather
  than only the server version.

See the [release process](operations/RELEASE_PROCESS.md) for the checks that
keep this table and published artifacts aligned.
