- **Per-agent EISV charts preserve measurement boundaries.** ODE startup values,
  behavioral estimates with continuity fallback inputs, and behavioral sensor
  estimates occupy separate source-labelled datasets. Isolated observations
  remain visible, and unknown or legacy-inferred sources are points only.
  Coherence follows its own producer and role, including the ODE control-feedback
  label. Tooltips show the observation timestamp and provenance.
- History responses carry response-local `chart_segments` for EISV and coherence,
  computed before chart sampling. A hidden source excursion or recorded
  instrument-version change therefore breaks the line even when the plotted
  endpoints have the same source. Older responses without these boundaries
  remain readable as points. This changes reporting only; estimator values,
  startup promotion, assessment, and policy are unchanged.
