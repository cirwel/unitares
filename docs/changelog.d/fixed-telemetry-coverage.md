- State reads now expose the live behavioral update count and a separately scoped
latest persisted assessment: its timestamp, age, evidence class and per-axis
reading coverage. Missing history, unavailable storage and missing coverage
remain distinct. Reading state does not create an observation.

- The host-observation endpoint preserves an optional `tool_unknown_status_delta`
for Codex tool outcomes. Unknown statuses are disjoint from known errors; older
producers that omit the field do not claim zero unknowns. Counts remain unscored
audit evidence and do not write EISV, predictions or liveness.
