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
        {Phoenix.PubSub, name: DialecticLive.PubSub}
      ] ++
        firehose_child() ++
        timeline_children() ++
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

  # The Timeline pane's feed, its fetch tasks and its ETS heir. Gated so tests
  # start their own instances with start_supervised. The feed makes no request
  # until a Timeline LiveView subscribes.
  defp timeline_children do
    if Keyword.get(Application.get_env(:dialectic_live, :timeline, []), :start_timeline_feed, false) do
      firehose =
        if firehose_child() == [], do: nil, else: DialecticLive.Firehose

      [
        DialecticLive.TimelineTableHeir,
        {Task.Supervisor, name: DialecticLive.TaskSupervisor},
        {DialecticLive.TimelineFeed,
         firehose: firehose,
         reread_ms: Keyword.get(Application.get_env(:dialectic_live, :timeline, []), :reread_ms, 60_000)}
      ]
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
