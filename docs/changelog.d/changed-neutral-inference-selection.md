- Interface contract 2.0.0 removes implicit vendor selection for omitted
  `delegate_inference.host_id` and thorough `consult` calls. Select a host
  explicitly, configure `jobs.consult.thorough.prefer` through
  `UNITARES_INFERENCE_HOSTS_FILE`, or provide one unambiguous eligible host.
  Ambiguous/unconfigured requests return actionable refusals; consultation
  retains caller-permitted local degradation. Explicit pins and councils keep
  their existing authorization and privacy rules. Updated SDKs omit an absent
  host instead of sending the old default. The document schema remains v1;
  `delegate_inference`’s input digest and the aggregate surface digest change.
- The same release changes `get_workspace_health` and
  `admin(action='workspace_health')` to installation diagnostics without desktop
  client discovery or assumed checkout documents. Default client mappings and
  server lists are empty; configured counts are zero and do not measure
  liveness. Unperformed security, executable-permission, live-server and
  documentation checks return `null`, with `not_assessed` or `not_requested`
  status where applicable. Clients must accept these unknown/unrequested
  states instead of treating them as failure or success. Explicit client and
  workspace-file checks remain Python embedding hooks, not MCP file access;
  use `health_check` for running server subsystem health. Tool inputs are
  unchanged for workspace diagnostics.
