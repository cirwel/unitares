defmodule DialecticLive.Application do
  # See https://elixir.hexdocs.pm/Application.html
  # for more information on OTP Applications
  @moduledoc false

  use Application

  @impl true
  def start(_type, _args) do
    children =
      [
        DialecticLiveWeb.Telemetry,
        {DNSCluster, query: Application.get_env(:dialectic_live, :dns_cluster_query) || :ignore},
        {Phoenix.PubSub, name: DialecticLive.PubSub}
      ] ++
        firehose_child() ++
        [
          # Start to serve requests, typically the last entry
          DialecticLiveWeb.Endpoint
        ]

    # See https://elixir.hexdocs.pm/Supervisor.html
    # for other strategies and supported options
    opts = [strategy: :one_for_one, name: DialecticLive.Supervisor]
    Supervisor.start_link(children, opts)
  end

  # The broadcaster WS client is gated on config so the app still boots when the
  # upstream governance firehose (:8767) is unreachable in this environment.
  defp firehose_child do
    if Keyword.get(Application.get_env(:dialectic_live, :governance, []), :start_firehose, false) do
      [DialecticLive.Firehose]
    else
      []
    end
  end

  # Tell Phoenix to update the endpoint configuration
  # whenever the application is updated.
  @impl true
  def config_change(changed, _new, removed) do
    DialecticLiveWeb.Endpoint.config_change(changed, removed)
    :ok
  end
end
