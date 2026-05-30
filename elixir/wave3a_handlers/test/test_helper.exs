ExUnit.start()

# Tests drive `Wave3aHandlers.HTTPRouter` via `Plug.Test` without booting
# Bandit. Application start is gated by `config/test.exs` setting
# `start_application: false`, so `mix test` won't auto-spawn the supervisor
# tree. Tests that need the supervisor (restart semantics) spin it up
# explicitly inside the test body.
