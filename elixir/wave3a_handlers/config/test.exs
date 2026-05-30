import Config

# Tests start the supervisor manually inside individual cases (the
# `Wave3aHandlers.SupervisorTest` exercises one_for_one restart explicitly),
# and the HTTP-router cases route via `Plug.Test` without booting Bandit. So
# the OTP `mod:` callback should NOT spin children at boot — same pattern as
# lease plane's `config/test.exs`.
config :wave3a_handlers,
  http_port: 8770,
  http_ip: {127, 0, 0, 1},
  start_application: false,
  start_http: false,
  probe_base_url: "http://127.0.0.1:8767",
  probe_timeout_ms: 500
