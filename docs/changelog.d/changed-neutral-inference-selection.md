- Interface contract 2.0.0 removes implicit vendor selection for omitted
  `delegate_inference.host_id` and thorough `consult` calls. Select a host
  explicitly, configure `jobs.consult.thorough.prefer` through
  `UNITARES_INFERENCE_HOSTS_FILE`, or provide one unambiguous eligible host.
  Ambiguous/unconfigured requests return actionable refusals; consultation
  retains caller-permitted local degradation. Explicit pins and councils keep
  their existing authorization and privacy rules. Updated SDKs omit an absent
  host instead of sending the old default. The document schema remains v1;
  `delegate_inference`’s input digest and the aggregate surface digest change.
