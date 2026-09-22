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

The package does **not** install, update, or start UNITARES and does not carry credentials.

## Prerequisite

Run UNITARES locally before enabling the plugin. The repository quickstart is:

```bash
v=$(curl -fsSL https://raw.githubusercontent.com/cirwel/unitares/master/PUBLISHED_VERSION)
git clone --branch "v$v" --depth 1 https://github.com/cirwel/unitares.git
cd unitares
docker compose up -d --wait
```

Then verify the MCP endpoint is reachable at `http://127.0.0.1:8767/mcp/`.

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
