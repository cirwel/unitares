defmodule AgentOrchestrator.MixProject do
  use Mix.Project

  def project do
    [
      app: :agent_orchestrator,
      version: "0.1.0",
      elixir: "~> 1.19",
      start_permanent: Mix.env() == :prod,
      elixirc_paths: elixirc_paths(Mix.env()),
      deps: deps()
    ]
  end

  def application do
    # :inets/:ssl provide the built-in :httpc client used by LeasePlaneClient,
    # so the orchestrator carries no third-party HTTP dependency. The lease
    # plane is localhost-only; :ssl is listed for httpc's startup contract,
    # not because the boundary is TLS.
    [
      extra_applications: [:logger, :inets, :ssl],
      mod: {AgentOrchestrator.Application, []}
    ]
  end

  defp elixirc_paths(:test), do: ["lib", "test/support"]
  defp elixirc_paths(_), do: ["lib"]

  defp deps do
    [
      {:jason, "~> 1.4"},
      # Envelope classifiers only (LeasePlaneEnvelope); transport stays :httpc.
      {:unitares_sdk, path: "../unitares_sdk"},
      # Control surface (lib/agent_orchestrator/http_router.ex). Plug 1.18+ for
      # the Plug.Parsers.ParseError shape the router's error handler matches;
      # Bandit is the localhost listener. Same stack as the lease plane.
      {:plug, "~> 1.18"},
      {:bandit, "~> 1.6"},
      # Durable keyed-spawn reservations in the shared governance Postgres.
      {:postgrex, "~> 0.20"},
      # Already present transitively (bandit, plug) — declared because we are now
      # a direct emitter, not because it adds anything to the dependency tree.
      {:telemetry, "~> 1.0"}
    ]
  end
end
