- **Antigravity host adapter:** keep the latest cumulative session counters
  when resuming a denied or truncated response. Earlier turns are no longer
  counted again, and cache/thinking counters remain separate. If a later reply
  omits usable usage, retain the last observed receipt; this does not establish
  complete accounting for that reply (#2811).
