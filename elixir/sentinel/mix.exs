defmodule UnitaresSentinel.MixProject do
  @moduledoc """
  Wave 1 RFC `docs/proposals/beam-wave-1-sentinel.md` v0.1.1 §Bootstrap spec
  (B5 reviewer council fold). Sibling app to `elixir/lease_plane/` —
  flat single-app project, NOT umbrella, matching existing topology
  (architect N4: umbrella promotion deferred to Wave 3+).
  """

  use Mix.Project

  def project do
    [
      app: :unitares_sentinel,
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
      mod: {UnitaresSentinel.Application, []}
    ]
  end

  defp elixirc_paths(:test), do: ["lib", "test/support"]
  defp elixirc_paths(_), do: ["lib"]

  defp deps do
    [
      # Postgrex for `lease_plane_events` polling (Surface 3).
      {:postgrex, "~> 0.20"},
      # JSON for findings emission (Surface 2 → POST /api/findings).
      {:jason, "~> 1.4"},
      # WebSocket consumer for `/ws/eisv` ingestion (B2 reviewer fold).
      {:mint_web_socket, "~> 1.0"},
      # HTTP client for `/api/findings` POSTs — Mint-based, hex.pm grade.
      {:finch, "~> 0.18"},
      # Property tests for fingerprint equivalence (Tier 2 contract).
      {:stream_data, "~> 0.6", only: :test}
    ]
  end
end
