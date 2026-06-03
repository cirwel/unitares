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
      {:jason, "~> 1.4"}
    ]
  end
end
