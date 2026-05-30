defmodule Wave3aHandlers.MixProject do
  @moduledoc """
  Wave 3a RFC `docs/proposals/beam-wave-3a-read-only-handlers.md` v0.2 §5
  PR #4 — first inbound-HTTP MCP listener on BEAM.

  Sibling app to `elixir/lease_plane/` and `elixir/sentinel/` (flat single-app
  project, NOT umbrella, matching existing topology). Per RFC §6 Q3 the
  long-term decision (this app stays separate forever vs. merges into the
  later Wave 3b OTP app) is deferred to Wave 3b operator review; PR #4 ships
  as a sibling app and that decision does not block this PR.

  Dependency choices mirror `elixir/lease_plane/mix.exs` plus `finch` for the
  outbound Python-probe client (the lease plane is inbound-only):

    * `bandit` — HTTP listener (matching lease plane Phase A)
    * `plug` — routing surface
    * `finch` — outbound HTTP client to `127.0.0.1:8767/v1/probe/*`
    * `jason` — JSON encode/decode

  No Postgrex dependency — PR #4 is the listener skeleton, no DB access on
  the BEAM side. Wave 3b may add one if the identity-middleware port lands
  here; the routing-table audit handler likewise stays Python-side.
  """

  use Mix.Project

  def project do
    [
      app: :wave3a_handlers,
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
      mod: {Wave3aHandlers.Application, []}
    ]
  end

  defp elixirc_paths(:test), do: ["lib", "test/support"]
  defp elixirc_paths(_), do: ["lib"]

  defp deps do
    [
      # Plug 1.18+ — same baseline as lease plane (Plug.Parsers.ParseError
      # shape relied on by typed-error envelope helpers).
      {:plug, "~> 1.18"},
      {:bandit, "~> 1.6"},
      # Outbound client to the Python probe surface. Finch is Mint-based and
      # the same library Sentinel already uses for /api/findings POSTs.
      {:finch, "~> 0.18"},
      {:jason, "~> 1.4"}
    ]
  end
end
