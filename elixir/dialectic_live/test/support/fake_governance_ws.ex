defmodule DialecticLive.Test.FakeGovernanceWS do
  @moduledoc """
  A stand-in for governance's `/ws/eisv`, served by Bandit on an ephemeral
  loopback port. `mode: :accept` upgrades and hands each connection to
  `DialecticLive.Test.FakeGovernanceWSHandler`, which reports to `:test` and
  pushes whatever text it is sent. `mode: {:status, code}` answers the upgrade
  with that HTTP status instead, the way a refusing server would.
  """

  @behaviour Plug

  @impl true
  def init(opts), do: opts

  @impl true
  def call(conn, opts) do
    case Keyword.get(opts, :mode, :accept) do
      {:status, status} ->
        conn |> Plug.Conn.send_resp(status, "refused") |> Plug.Conn.halt()

      :accept ->
        conn
        |> WebSockAdapter.upgrade(
          DialecticLive.Test.FakeGovernanceWSHandler,
          %{test: Keyword.fetch!(opts, :test)},
          []
        )
        |> Plug.Conn.halt()
    end
  end

  @doc "Start a server under the test supervisor; returns its `ws://` URL."
  def start!(opts) do
    pid =
      ExUnit.Callbacks.start_supervised!(
        {Bandit, plug: {__MODULE__, opts}, ip: :loopback, port: 0, startup_log: false},
        id: make_ref()
      )

    {:ok, {_ip, port}} = ThousandIsland.listener_info(pid)
    "ws://127.0.0.1:#{port}/ws/eisv"
  end

  @doc "A loopback port with nothing listening on it."
  def closed_port do
    {:ok, socket} = :gen_tcp.listen(0, ip: {127, 0, 0, 1})
    {:ok, port} = :inet.port(socket)
    :ok = :gen_tcp.close(socket)
    port
  end
end
