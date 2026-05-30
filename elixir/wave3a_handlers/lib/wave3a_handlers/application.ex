defmodule Wave3aHandlers.Application do
  @moduledoc """
  OTP entry point for the Wave 3a BEAM handler app.

  Mirrors `UnitaresLeasePlane.Application`'s shape: bearer token sourced
  from env at boot (`WAVE_3A_BEAM_TOKEN`); probe token sourced from env at
  boot (`WAVE_3A_PROBE_TOKEN`). Both stash into Application env; HTTPAuth
  reads `:beam_token` at request time. Fail-closed: if `WAVE_3A_BEAM_TOKEN`
  is unset the listener still starts but every authed route returns 503
  per RFC §2.5.

  ## HTTP bind discipline

  Defaults to IPv4 `127.0.0.1:8770`. The Python proxy at
  `src/wave3a_beam_proxy.py` hits a dotted-quad URL passed in by the
  routing-table config (no DNS in the path). Off-host exposure is
  intentionally not a built-in option — the bearer-auth fail-closed posture
  assumes a single trust boundary at `localhost`.

  ## Supervisor topology

  `one_for_one`. Two children (when `start_application: true` AND
  `start_http: true`):

    * `{Finch, name: Wave3aHandlers.ProbeFinch}` — connection pool for the
      outbound probe client. Wave 3a does not exercise this yet (no handler
      is wired into the dispatch table in PR #4), but the supervisor brings
      it up so PR #5 can land a single-line handler without touching the
      supervisor tree.
    * `{Bandit, plug: Wave3aHandlers.HTTPRouter, ip: ..., port: ...}` —
      the inbound HTTP listener. First inbound-HTTP MCP listener on BEAM
      in the fleet (Sentinel is outbound-Finch-only, lease plane is
      coordination-traffic-only).

  Test mode (`config_env() == :test` or `start_application: false`) starts
  the supervisor with no children so individual tests can drive
  `Wave3aHandlers.HTTPRouter` via `Plug.Test` or spin up the supervisor
  manually for restart-semantics coverage.
  """

  use Application

  require Logger

  @impl true
  def start(_type, _args) do
    if Application.get_env(:wave3a_handlers, :start_application, true) do
      start_full()
    else
      Supervisor.start_link([], strategy: :one_for_one, name: Wave3aHandlers.Supervisor)
    end
  end

  defp start_full do
    if token = System.get_env("WAVE_3A_BEAM_TOKEN") do
      Application.put_env(:wave3a_handlers, :beam_token, token)
    end

    if token = System.get_env("WAVE_3A_PROBE_TOKEN") do
      Application.put_env(:wave3a_handlers, :probe_token, token)
    end

    children = [{Finch, name: Wave3aHandlers.ProbeFinch}] ++ http_children()

    opts = [strategy: :one_for_one, name: Wave3aHandlers.Supervisor]
    Supervisor.start_link(children, opts)
  end

  defp http_children do
    if Application.get_env(:wave3a_handlers, :start_http, true) do
      port = Application.get_env(:wave3a_handlers, :http_port, 8770)
      ip = Application.get_env(:wave3a_handlers, :http_ip, {127, 0, 0, 1})

      Logger.info(
        "[wave3a_handlers] starting Bandit listener at #{inspect(ip)}:#{port}"
      )

      [
        Supervisor.child_spec(
          {Bandit, plug: Wave3aHandlers.HTTPRouter, ip: ip, port: port},
          id: Wave3aHandlers.HTTPListener
        )
      ]
    else
      []
    end
  end
end
