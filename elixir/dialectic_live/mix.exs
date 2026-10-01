defmodule DialecticLive.MixProject do
  use Mix.Project

  def project do
    [
      app: :dialectic_live,
      version: "0.1.0",
      elixir: "~> 1.15",
      elixirc_paths: elixirc_paths(Mix.env()),
      start_permanent: Mix.env() == :prod,
      aliases: aliases(),
      deps: deps(),
      compilers: [:phoenix_live_view] ++ Mix.compilers(),
      listeners: [Phoenix.CodeReloader]
    ]
  end

  # Configuration for the OTP application.
  #
  # Type `mix help compile.app` for more information.
  def application do
    [
      mod: {DialecticLive.Application, []},
      extra_applications: [:logger, :runtime_tools]
    ]
  end

  def cli do
    [
      preferred_envs: [precommit: :test]
    ]
  end

  # Specifies which paths to compile per environment.
  defp elixirc_paths(:test), do: ["lib", "test/support"]
  defp elixirc_paths(_), do: ["lib"]

  # Specifies your project dependencies.
  #
  # Type `mix help deps` for examples and options.
  defp deps do
    [
      {:phoenix, "~> 1.8.8"},
      {:phoenix_html, "~> 4.1"},
      {:phoenix_live_reload, "~> 1.2", only: :dev},
      {:phoenix_live_view, "~> 1.2.0"},
      {:lazy_html, ">= 0.1.0", only: :test},
      {:phoenix_live_dashboard, "~> 0.8.3"},
      {:esbuild, "~> 0.10", runtime: Mix.env() == :dev},
      {:tailwind, "~> 0.3", runtime: Mix.env() == :dev},
      {:heroicons,
       github: "tailwindlabs/heroicons",
       tag: "v2.2.0",
       sparse: "optimized",
       app: false,
       compile: false,
       depth: 1},
      {:telemetry_metrics, "~> 1.0"},
      {:telemetry_poller, "~> 1.0"},
      {:gettext, "~> 1.0"},
      {:jason, "~> 1.2"},
      {:dns_cluster, "~> 0.2.0"},
      {:bandit, "~> 1.5"},
      # Outbound clients to the Python governance MCP (:8767):
      # mint_web_socket consumes the broadcaster firehose (/ws/eisv), same lib as the
      # Elixir Sentinel; req issues the /v1/tools/call POSTs (dialectic list/get).
      {:mint_web_socket, "~> 1.0"},
      {:req, "~> 0.5"},
      # The shared governance response contract. Transport stays Req — the SDK
      # is consumed for `Envelope` only, because what four BEAM clients got
      # wrong was never the HTTP call, it was reading the reply.
      {:unitares_sdk, path: "../unitares_sdk"}
    ]
  end

  # Aliases are shortcuts or tasks specific to the current project.
  # For example, to install project dependencies and perform other setup tasks, run:
  #
  #     $ mix setup
  #
  # See the documentation for `Mix` for more info on aliases.
  defp aliases do
    [
      setup: ["deps.get", "assets.setup", "assets.build"],
      "assets.setup": ["tailwind.install --if-missing", "esbuild.install --if-missing"],
      "assets.build": ["compile", "tailwind dialectic_live", "esbuild dialectic_live"],
      # scripts/build-assets.sh runs these two steps separately: a failed
      # compile leaves the previous digest servable, a failed digest does not.
      "assets.compile": [
        "tailwind dialectic_live --minify",
        "esbuild dialectic_live --minify"
      ],
      # Wraps Phoenix's own phx.digest (an alias may call the task it is
      # named after), so every digest in this project goes through it: only
      # scripts/build-assets.sh may run one (it holds the build lock), and
      # the marker saying the digest finished is cleared first and written
      # only on success. See clear_digest_marker/1.
      "phx.digest": [&clear_digest_marker/1, "phx.digest", &write_digest_marker/1],
      "assets.deploy": ["assets.compile", "phx.digest"],
      precommit: ["compile --warnings-as-errors", "deps.unlock --unused", "format", "test"]
    ]
  end

  # Read by scripts/build-assets.sh (DIGEST_OK); keep the two paths in step.
  @digest_marker Path.join(__DIR__, "_build/assets-digest.ok")

  defp clear_digest_marker(_args) do
    if System.get_env("DIALECTIC_LIVE_ASSETS_LOCKED") != "1" do
      Mix.raise(
        "build assets with scripts/build-assets.sh (--strict to fail on any error); " <>
          "it serializes builds and owns the digest marker, so mix assets.deploy " <>
          "and phx.digest do not run on their own"
      )
    end

    case File.rm(@digest_marker) do
      :ok ->
        :ok

      {:error, :enoent} ->
        :ok

      {:error, reason} ->
        Mix.raise("could not clear #{@digest_marker}: #{:file.format_error(reason)}")
    end
  end

  defp write_digest_marker(_args) do
    File.mkdir_p!(Path.dirname(@digest_marker))
    File.touch!(@digest_marker)
  end
end
