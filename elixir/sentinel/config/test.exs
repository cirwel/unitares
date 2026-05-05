import Config

# Skeleton-stage: skip starting the application supervisor in tests.
# Test files exercise pure modules (e.g. AtomicWrite) without needing
# the supervisor tree. Postgrex sandbox + supervisor children land in
# follow-up PRs as the Surface 1–5 wires arrive.
config :unitares_sentinel, start_application: false
