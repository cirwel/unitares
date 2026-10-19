# Reviewed deployment environment overlays

`scripts/ops/deploy-mcp.sh` requires an environment overlay owned by the
deployment, outside the product repository. Supply both the file path and the
SHA-256 digest approved with that file's exact contents:

```bash
export UNITARES_MCP_ENV_OVERLAY=/path/to/deployment/governance-mcp.env
export UNITARES_MCP_ENV_OVERLAY_SHA256="<reviewed-64-character-sha256>"
./scripts/ops/deploy-mcp.sh
```

Record the expected digest in the deployment's reviewed configuration. Computing
a new digest automatically immediately before each deploy would accept an
unreviewed edit and defeat the check. An empty reviewed file is valid when no
overrides are needed; omitting either variable is an error.

The file contains uppercase `KEY=VALUE` lines. Comments occupy their own lines;
shell quoting, expansion, and inline comments are not supported. Keep secrets
private and apply the deployment's review requirements to settings that affect
authentication or trusted evidence. The digest proves correspondence with the
selected bytes, not who reviewed or authorized them.

Before taking the deployment lock, moving the checkout, inspecting migrations,
or changing services, the deploy verifies the file's syntax and digest. Every
later overlay read checks the same expected digest; the applier hashes and
parses one byte buffer. Replacing the file during deployment cannot substitute
unreviewed settings. Later failures retain the existing rollback or schema-078
forward-recovery rules.

The overlay updates only listed keys, preserves other plist values, and creates
a private write-once backup before changing the plist. Reload detection is
unchanged. It cannot switch the runtime database after migration preflight: the
deploy checks database equality again before restarting.

For a read-only check of the overlay, without a plist or deployment effects:

```bash
python3 scripts/ops/apply_plist_env_overlay.py \
  --overlay "$UNITARES_MCP_ENV_OVERLAY" \
  --expected-sha256 "$UNITARES_MCP_ENV_OVERLAY_SHA256" --validate-overlay
```

The generic applier still accepts `--plist PATH --overlay PATH` without a
digest for existing callers. The managed MCP deployment always requires one.
