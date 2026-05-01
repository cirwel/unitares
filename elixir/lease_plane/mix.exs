defmodule UnitaresLeasePlane.MixProject do
  use Mix.Project

  def project do
    [
      app: :lease_plane,
      version: "0.1.0",
      elixir: "~> 1.19",
      start_permanent: Mix.env() == :prod,
      elixirc_paths: elixirc_paths(Mix.env()),
      deps: deps()
    ]
  end

  def application do
    [
      extra_applications: [:logger],
      mod: {UnitaresLeasePlane.Application, []}
    ]
  end

  defp elixirc_paths(:test), do: ["lib", "test/support"]
  defp elixirc_paths(_), do: ["lib"]

  defp deps do
    [
      {:postgrex, "~> 0.20"},
      {:jason, "~> 1.4"}
      # HTTP layer in the next iteration:
      # {:plug, "~> 1.16"},
      # {:bandit, "~> 1.6"}
    ]
  end
end
