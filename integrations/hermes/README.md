# UNITARES for Hermes Agent

This directory is a portable [Agent Plugins v1](https://agent-plugins.org/) package for Hermes Agent.

It deliberately keeps the integration boundary small:

- Hermes remains the agent runtime.
- UNITARES remains a separately operated accountability runtime.
- The plugin connects Hermes to UNITARES over Streamable HTTP MCP.
- A focused skill teaches the Hermes agent the UNITARES session and discovery conventions.

## What the plugin installs

The package registers one MCP server named `unitares` at the standard loopback endpoint:

`http://127.0.0.1:8767/mcp/`

It also bundles the `unitares-governance` skill.

The MCP entry carries four static, non-secret provenance headers:

| Header | Value | Why |
|---|---|---|
| `X-Unitares-Harness-Type` | `hermes` | Names the harness, independent of which model provider Hermes is using |
| `X-Unitares-Harness-Source` | `caller_declared` | The value is package configuration, not something UNITARES observed |
| `X-Unitares-Adapter-Type` | `unitares-hermes-plugin` | Names the integration that delivered the observation |
| `X-Unitares-Adapter-Version` | the `plugin.json` version | Lets reports separate package revisions |

These headers are descriptive context only. UNITARES never uses them as identity proof, verdict authority, or a policy key (see `docs/operations/model-harness-risk-cohorts.md`), and they do not enter the transport fingerprint.

## Model providers

Hermes can run on several model providers, including API-key providers, OpenAI Codex sign-in (`openai-codex`), and Claude subscription providers. The harness is still Hermes in every case. That is why the harness is pinned in a header rather than inferred:

- Hermes provider ids such as `openai-codex`, `claude`, and `claude-code` look like other harness names. UNITARES' S22 comparison normalizes those strings to the Codex CLI and Claude Code harnesses, so a Hermes check-in that reported its provider as its harness would be filed under the wrong harness. The header wins over request-body harness claims, which closes that path.
- Some Hermes runtimes hand the tool loop to another agent process (the Codex app-server runtime and a proposed Claude Agent SDK runtime). If that process makes the MCP call, its User-Agent names Codex or Claude Code. A configured harness header still takes precedence over User-Agent detection, as long as the runtime forwards the configured headers. Confirm it does before relying on this for a given runtime.

The model changes with the provider, so it cannot be a static header. The bundled skill asks the agent to report `model_provider` and `model` in `sync_state`'s `provenance_context`. Those values are recorded as caller-declared and never enter an exact-model cohort.

The package does **not** install, update, or start UNITARES and does not carry credentials.

## Prerequisite

Run UNITARES locally before enabling the plugin. Follow the repository's pinned-release quickstart from the root README, then verify the MCP endpoint is reachable at:

`http://127.0.0.1:8767/mcp/`

## Test directly from GitHub

Before catalog admission, install this subdirectory as a custom plugin:

```bash
hermes plugins install https://github.com/cirwel/unitares#integrations/hermes --no-enable
hermes plugins enable unitares
```

For reproducible testing of a review candidate, pass an exact 40-character commit SHA with Hermes' `--ref` option.

## Authentication

The checked-in `mcp.json` intentionally contains no secrets. It targets UNITARES' default local loopback MCP endpoint.

If an operator enables bearer authentication on UNITARES, configure the authenticated remote MCP connection in Hermes using the operator's secret-management path rather than committing a token here.

## Catalog intent

Once this package passes Hermes validation and a live connection smoke test, the intended catalog entry is a community `tools` plugin pointing at the `integrations/hermes` subdirectory and pinned to the reviewed UNITARES commit SHA.
