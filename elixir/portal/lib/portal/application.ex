defmodule Portal.Application do
  # See https://elixir.hexdocs.pm/Application.html
  # for more information on OTP Applications
  @moduledoc false

  use Application

  @impl true
  def start(_type, _args) do
    # Refuse to boot on anything but loopback (portal-hub-v0 §3.2).
    endpoint = Application.get_env(:portal, PortalWeb.Endpoint, [])
    Portal.Boundary.check_bind!(get_in(endpoint, [:http, :ip]) || {127, 0, 0, 1})

    # No clustering: the hub is a lone, undistributed node.
    children = [
      PortalWeb.Telemetry,
      {Phoenix.PubSub, name: Portal.PubSub},
      # Start a worker by calling: Portal.Worker.start_link(arg)
      # {Portal.Worker, arg},
      # Start to serve requests, typically the last entry
      PortalWeb.Endpoint
    ]

    # See https://elixir.hexdocs.pm/Supervisor.html
    # for other strategies and supported options
    opts = [strategy: :one_for_one, name: Portal.Supervisor]
    Supervisor.start_link(children, opts)
  end

  # Tell Phoenix to update the endpoint configuration
  # whenever the application is updated.
  @impl true
  def config_change(changed, _new, removed) do
    PortalWeb.Endpoint.config_change(changed, removed)
    :ok
  end
end
