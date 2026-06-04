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
  acquires a `remote_heartbeat` lease on its `agent:<id>` surface when it spawns
  and releases it on exit, so the fleet plane has a single source of truth for
  "which ephemeral agents are live" and orphans self-heal via the reaper's TTL.
  """

  use Application

  @impl true
  def start(_type, _args) do
    children = [
      {Registry, keys: :unique, name: AgentOrchestrator.Registry},
      AgentOrchestrator.ResultStore,
      AgentOrchestrator.AgentSupervisor
    ]

    opts = [strategy: :one_for_one, name: AgentOrchestrator.Supervisor]
    Supervisor.start_link(children, opts)
  end
end
