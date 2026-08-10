defmodule UnitaresSdk.MixProject do
  use Mix.Project

  @version "0.1.0"

  def project do
    [
      app: :unitares_sdk,
      version: @version,
      # Deliberately the LOOSEST floor any current consumer can satisfy.
      # anima_broker and dialectic_live declare ~> 1.15; everything else is
      # ~> 1.19. A federation contract must not force its own consumers to
      # upgrade to adopt it, so the SDK targets the lowest common floor and
      # uses Erlang's :json (OTP 27+) rather than Elixir's JSON (1.18+).
      elixir: "~> 1.15",
      start_permanent: Mix.env() == :prod,
      deps: deps(),
      description:
        "Elixir client contract for the UNITARES governance tool bridge " <>
          "(POST /v1/tools/call) — transport, envelope, identity, backoff.",
      package: package(),
      docs: [main: "UnitaresSdk"]
    ]
  end

  def application do
    # :inets provides :httpc; :ssl is needed the moment a consumer points at
    # an https governance URL (gov.cirwel.org). Both ship with OTP — the SDK
    # has, and should keep, zero third-party runtime dependencies.
    [extra_applications: [:logger, :inets, :ssl]]
  end

  defp deps do
    # No runtime deps. See the repo execution-cost policy: nothing on the
    # required path may pull a metered or third-party service.
    []
  end

  defp package do
    [
      licenses: ["Apache-2.0"],
      links: %{"Source" => "https://github.com/cirwel/unitares"}
    ]
  end
end
