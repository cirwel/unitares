defmodule AgentOrchestrator.Application do
  @moduledoc """
  OTP entry point for the ephemeral-agent orchestrator.

  This is the BEAM-native fleet runtime axis of the BEAM footprint: it is NOT
  the governance-server migration (Wave 1-3, `beam-footprint-roadmap-v0.md`) and
  it is NOT the lease plane itself. It supervises *ephemeral agents* — short-lived
  external runtimes (a Claude SDK process, `claude -p`, a tool worker) — as
  OTP children, one `AgentRunner` GenServer per agent, each wrapping a `Port`.

  Topology:

      AgentOrchestrator.Supervisor            (one_for_one)
      ├── Registry  (AgentOrchestrator.Registry)   agent_id -> runner pid
      ├── ResultStore  (GenServer + ETS)            retained final results
      └── AgentSupervisor  (DynamicSupervisor)
          └── AgentRunner  (GenServer + Port)       restart: :temporary

  `ResultStore` starts before `AgentSupervisor` so its ETS table exists before
  any runner can finalize and write its result — closing the await-vs-fast-exit
  race (#581) where a finished agent's result was lost to `:not_found`.

  Lease-binding to the plane is the architectural-coherence payoff: an agent
  registers an `agent:/<id>` presence row when it spawns and releases it on
  exit, so the fleet plane has a single source of truth for "which ephemeral
  agents are live." The presence row uses the `remote_heartbeat` holder kind,
  which migration 042 (PR #588) routes to the self-healing TTL-row path — so an
  orphan left by a crash that skips `terminate/2` reaps itself at TTL rather
  than leaking indefinitely. Explicit release on exit remains the fast path; the
  TTL is the backstop, not the normal path. (Earlier drafts claimed self-heal
  for every scheme — that was false before #588, when only `file://` got the
  TTL-row path and other schemes were coerced to an auto-renewing holder. See
  `AgentRunner`'s moduledoc for the full corrected story.)
  """

  use Application

  @impl true
  def start(_type, _args) do
    # Bearer for the control surface, sourced from env at boot. Absent → HTTPAuth
    # returns 503 on every request (fail closed, never silently open). Mirrors
    # the lease plane's posture; matters more here because POST /v1/agents spawns
    # an OS process — an unauthenticated reach is RCE.
    if token = System.get_env("AGENT_ORCHESTRATOR_BEARER_TOKEN") do
      Application.put_env(:agent_orchestrator, :bearer_token, token)
    end

    children =
      [
        {Registry, keys: :unique, name: AgentOrchestrator.Registry},
        AgentOrchestrator.ResultStore,
        AgentOrchestrator.AgentSupervisor
      ] ++ http_children()

    opts = [strategy: :one_for_one, name: AgentOrchestrator.Supervisor]
    Supervisor.start_link(children, opts)
  end

  # Localhost-only Bandit listener for the control surface. Gated by
  # :start_http (OFF under :test so the unit suite binds no socket). IPv4 only —
  # Bandit does not bind ::1, and the localhost trust boundary is the security
  # model, so off-host exposure is intentionally not a built-in option.
  defp http_children do
    if Application.get_env(:agent_orchestrator, :start_http, true) do
      port = Application.get_env(:agent_orchestrator, :http_port, 8789)
      ip = Application.get_env(:agent_orchestrator, :http_ip, {127, 0, 0, 1})

      [{Bandit, plug: AgentOrchestrator.HTTPRouter, ip: ip, port: port}]
    else
      []
    end
  end
end
