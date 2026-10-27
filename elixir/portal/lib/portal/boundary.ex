defmodule Portal.Boundary do
  @moduledoc """
  The hub's network boundary (portal-hub-v0 §3.2): a loopback bind, one
  allowed Host and port, and one allowed Origin, all derived from
  `config :portal, :boundary`.

  The hub will start and resume agent sessions, so it answers only on
  loopback, and the application refuses to boot on any wider address rather
  than serve on it. Loopback alone does not stop a local process, so every
  page also sits behind `PortalWeb.Plugs.RequireOperator`.
  """

  @doc "The one `{host, port}` the hub answers for."
  def allowed do
    config = Application.fetch_env!(:portal, :boundary)
    {Keyword.fetch!(config, :host), Keyword.fetch!(config, :port)}
  end

  @doc "The one Origin a LiveView socket may come from."
  def origin do
    {host, port} = allowed()
    "http://#{host}:#{port}"
  end

  @doc "Raise unless `ip` is a loopback address."
  def check_bind!(ip) do
    if loopback?(ip) do
      :ok
    else
      raise ArgumentError,
            "the portal binds loopback only and refuses to boot on #{inspect(ip)}"
    end
  end

  def loopback?({127, _, _, _}), do: true
  def loopback?({0, 0, 0, 0, 0, 0, 0, 1}), do: true
  def loopback?(:loopback), do: true
  def loopback?(_), do: false
end
