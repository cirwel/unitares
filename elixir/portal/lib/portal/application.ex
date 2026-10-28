defmodule Portal.Application do
  # See https://elixir.hexdocs.pm/Application.html
  # for more information on OTP Applications
  @moduledoc false

  use Application

  @impl true
  def start(_type, _args) do
    # Refuse to boot on anything but loopback (portal-hub-v0 §3.2).
    Portal.Boundary.check_endpoint!(Application.get_env(:portal, PortalWeb.Endpoint, []))

    # No clustering: the hub is a lone, undistributed node.
    children =
      [
        PortalWeb.Telemetry,
        {Phoenix.PubSub, name: Portal.PubSub}
      ] ++
        timeline_children() ++
        [
          # Start to serve requests, typically the last entry
          PortalWeb.Endpoint
        ]

    # See https://elixir.hexdocs.pm/Supervisor.html
    # for other strategies and supported options
    opts = [strategy: :one_for_one, name: Portal.Supervisor]
    Supervisor.start_link(children, opts)
  end

  # The timeline's feed, its fetch tasks, its ETS heir and the stream client,
  # under a supervisor of their own, so a governance outage or a reconnect
  # storm stays inside it and never reaches the endpoint or the sign-in. Gated
  # so tests start their own instances. The feed makes no request until a
  # timeline LiveView subscribes, and the hub boots with governance down.
  defp timeline_children do
    timeline = Application.get_env(:portal, :timeline, [])

    if Keyword.get(timeline, :start_timeline_feed, false) do
      firehose? = Keyword.get(timeline, :start_firehose, false)

      children =
        if(firehose?, do: [Portal.Firehose], else: []) ++
          [
            Portal.TimelineTableHeir,
            {Task.Supervisor, name: Portal.TaskSupervisor},
            {Portal.TimelineFeed,
             firehose: if(firehose?, do: Portal.Firehose),
             reread_ms: Keyword.get(timeline, :reread_ms, 60_000)}
          ]

      [
        %{
          id: Portal.TimelineSupervisor,
          type: :supervisor,
          start:
            {Supervisor, :start_link,
             [children, [strategy: :one_for_one, name: Portal.TimelineSupervisor]]}
        }
      ]
    else
      []
    end
  end

  # Tell Phoenix to update the endpoint configuration
  # whenever the application is updated.
  @impl true
  def config_change(changed, _new, removed) do
    PortalWeb.Endpoint.config_change(changed, removed)
    :ok
  end
end
