defmodule DialecticLive.Firehose do
  @moduledoc """
  Consumer of the Python broadcaster firehose (`/ws/eisv` on :8767), re-published
  onto `Phoenix.PubSub` so LiveViews can subscribe locally.

  This mirrors the Elixir Sentinel's `EISVWebSocket` (Mint.WebSocket, no
  application heartbeat, reconnect after any failure). The difference is the sink:
  Sentinel ingests into FleetState; we fan each decoded JSON event onto PubSub.

  Topics:
    * `"governance:events"` — every event, as `{:governance_event, map}`
    * `"dialectic:events"`   — only events whose `type` starts with `"dialectic"`
      (the server has emitted `dialectic_*` events since 2026-06-29, #1251)
    * `"firehose:status"`    — `{:firehose_status, status}` on each change of
      connection state, where status is `:connected`, `:unreachable` (nothing
      answered, or the stream dropped) or `:refused` (the upgrade was answered
      401/403). It is a separate topic so the dialectic pane, which treats every
      `"dialectic:events"` message as a doorbell, never sees it.

  Reconnects back off from 1 s, doubling to a 10 s ceiling, and reset to 1 s
  after a successful upgrade. The upgrade sends no auth header: on the live
  server it is accepted through the local-posture trusted-network bypass, and
  client credential resolution is a later, operator-reserved milestone (portal
  proposal §3.5, M4).

  Options: `:url`, `:name` (default `#{inspect(__MODULE__)}`), `:pubsub`
  (default `DialecticLive.PubSub`), `:reconnect_min_ms`, `:reconnect_max_ms`
  (`:reconnect_ms` is accepted as the ceiling), `:connect_timeout_ms`.
  """

  use GenServer
  require Logger

  @pubsub DialecticLive.PubSub
  @all_topic "governance:events"
  @dialectic_topic "dialectic:events"
  @status_topic "firehose:status"

  @default_reconnect_min_ms 1_000
  @default_reconnect_max_ms 10_000
  @default_connect_timeout_ms 5_000

  defstruct url: nil,
            pubsub: @pubsub,
            reconnect_min_ms: @default_reconnect_min_ms,
            reconnect_max_ms: @default_reconnect_max_ms,
            backoff_ms: @default_reconnect_min_ms,
            connect_timeout_ms: @default_connect_timeout_ms,
            reconnect_timer: nil,
            conn: nil,
            websocket: nil,
            ref: nil,
            connected?: false,
            status: :connecting,
            upgrade_status: nil,
            upgrade_headers: []

  def all_topic, do: @all_topic
  def dialectic_topic, do: @dialectic_topic
  def status_topic, do: @status_topic

  def child_spec(opts) do
    %{
      id: Keyword.get(opts, :name, __MODULE__),
      start: {__MODULE__, :start_link, [opts]}
    }
  end

  def start_link(opts \\ []) do
    GenServer.start_link(__MODULE__, opts, name: Keyword.get(opts, :name, __MODULE__))
  end

  def connected?(server \\ __MODULE__), do: GenServer.call(server, :connected?)

  @doc "The current connection status: `:connecting`, `:connected`, `:unreachable` or `:refused`."
  def status(server \\ __MODULE__), do: GenServer.call(server, :status)

  @doc false
  def backoff_ms(server \\ __MODULE__), do: GenServer.call(server, :backoff_ms)

  @impl true
  def init(opts) do
    cfg = Application.get_env(:dialectic_live, :governance, [])
    min_ms = Keyword.get(opts, :reconnect_min_ms, @default_reconnect_min_ms)
    max_ms = Keyword.get(opts, :reconnect_max_ms, Keyword.get(opts, :reconnect_ms, @default_reconnect_max_ms))

    state = %__MODULE__{
      url: Keyword.get(opts, :url) || Keyword.get(cfg, :ws_url),
      pubsub: Keyword.get(opts, :pubsub, @pubsub),
      reconnect_min_ms: min_ms,
      reconnect_max_ms: max(max_ms, min_ms),
      backoff_ms: min_ms,
      connect_timeout_ms: Keyword.get(opts, :connect_timeout_ms, @default_connect_timeout_ms)
    }

    {:ok, state, {:continue, :connect}}
  end

  @impl true
  def handle_continue(:connect, state), do: connect(state)

  @impl true
  def handle_call(:connected?, _from, state), do: {:reply, state.connected?, state}
  def handle_call(:status, _from, state), do: {:reply, state.status, state}
  def handle_call(:backoff_ms, _from, state), do: {:reply, state.backoff_ms, state}

  @impl true
  def handle_info(:connect, state), do: connect(%{state | reconnect_timer: nil})

  def handle_info(message, %{conn: nil} = state) do
    Logger.debug("Firehose ignoring pre-connect message: #{inspect(message)}")
    {:noreply, state}
  end

  def handle_info(message, state) do
    case Mint.WebSocket.stream(state.conn, message) do
      {:ok, conn, responses} ->
        state |> Map.put(:conn, conn) |> handle_responses(responses)

      {:error, conn, reason, responses} ->
        state =
          state |> Map.put(:conn, conn) |> handle_responses_without_reply(responses)

        {:noreply, fail(state, :unreachable, "Firehose stream failed: #{inspect(reason)}")}

      :unknown ->
        {:noreply, state}
    end
  end

  # --- connection ---

  defp connect(%{url: url} = state) when is_binary(url) and url != "" do
    uri = URI.parse(url)

    with {:ok, schemes} <- schemes(uri),
         {:ok, host} <- host(uri),
         {:ok, conn} <-
           Mint.HTTP.connect(schemes.http, host, port(uri, schemes.ws),
             protocols: [:http1],
             mode: :active,
             timeout: state.connect_timeout_ms
           ),
         {:ok, conn, ref} <- Mint.WebSocket.upgrade(schemes.ws, conn, path(uri), []) do
      {:noreply,
       %{
         state
         | conn: conn,
           ref: ref,
           websocket: nil,
           connected?: false,
           upgrade_status: nil,
           upgrade_headers: []
       }}
    else
      {:error, conn, reason} ->
        close_conn(conn)
        {:noreply, fail(%{state | conn: nil}, :unreachable, unreachable_message(reason))}

      {:error, %Mint.TransportError{} = reason} ->
        {:noreply, fail(state, :unreachable, unreachable_message(reason))}

      {:error, reason} ->
        # A malformed ws_url: retrying cannot fix it, but backing off keeps the
        # log readable and lets a config reload take effect.
        {:noreply, fail(state, :unreachable, "Firehose config failed: #{inspect(reason)}")}
    end
  end

  defp connect(state) do
    Logger.warning("Firehose has no ws_url configured; not connecting")
    {:noreply, state}
  end

  defp unreachable_message(%Mint.TransportError{reason: :econnrefused}),
    do: "Firehose: governance unreachable (connection refused)"

  defp unreachable_message(%Mint.TransportError{reason: reason}),
    do: "Firehose: governance unreachable (#{inspect(reason)})"

  defp unreachable_message(reason), do: "Firehose: governance unreachable (#{inspect(reason)})"

  defp handle_responses(state, responses) do
    {:noreply, handle_responses_without_reply(state, responses)}
  end

  defp handle_responses_without_reply(state, responses) do
    Enum.reduce(responses, state, &handle_response/2)
  end

  defp handle_response({:status, ref, status}, %{ref: ref} = state),
    do: %{state | upgrade_status: status}

  defp handle_response({:headers, ref, headers}, %{ref: ref} = state),
    do: %{state | upgrade_headers: headers}

  defp handle_response({:done, ref}, %{ref: ref, websocket: nil, upgrade_status: status} = state)
       when status in [401, 403] do
    fail(state, :refused, "Firehose: governance refused credentials (HTTP #{status} on upgrade)")
  end

  defp handle_response({:done, ref}, %{ref: ref, websocket: nil} = state) do
    case Mint.WebSocket.new(state.conn, ref, state.upgrade_status, state.upgrade_headers) do
      {:ok, conn, websocket} ->
        Logger.info("Firehose connected to #{state.url}")

        %{state | conn: conn, websocket: websocket, connected?: true, backoff_ms: state.reconnect_min_ms}
        |> set_status(:connected)

      {:error, conn, reason} ->
        fail(%{state | conn: conn}, :unreachable, "Firehose upgrade failed: #{inspect(reason)}")
    end
  end

  defp handle_response({:done, ref}, %{ref: ref} = state) do
    fail(state, :unreachable, "Firehose stream closed")
  end

  defp handle_response({:data, ref, data}, %{ref: ref, websocket: ws} = state)
       when not is_nil(ws) do
    case Mint.WebSocket.decode(ws, data) do
      {:ok, ws, frames} ->
        state |> Map.put(:websocket, ws) |> handle_frames(frames)

      {:error, ws, reason} ->
        fail(%{state | websocket: ws}, :unreachable, "Firehose decode failed: #{inspect(reason)}")
    end
  end

  defp handle_response({:error, ref, reason}, %{ref: ref} = state) do
    fail(state, :unreachable, "Firehose response error: #{inspect(reason)}")
  end

  defp handle_response(_response, state), do: state

  defp handle_frames(state, frames), do: Enum.reduce(frames, state, &handle_frame/2)

  # A frame after a reconnect was scheduled belongs to a closed connection.
  defp handle_frame(_frame, %{conn: nil} = state), do: state

  defp handle_frame({:text, message}, state) do
    publish(state.pubsub, message)
    state
  end

  defp handle_frame({:ping, payload}, state), do: send_frame(state, {:pong, payload})

  defp handle_frame({:close, code, reason}, state) do
    fail(state, :unreachable, "Firehose closed by peer: #{inspect({code, reason})}")
  end

  defp handle_frame({:error, reason}, state) do
    fail(state, :unreachable, "Firehose frame error: #{inspect(reason)}")
  end

  defp handle_frame(_frame, state), do: state

  # --- sink: decode and fan onto PubSub ---

  defp publish(pubsub, message) when is_binary(message) do
    case Jason.decode(message) do
      {:ok, %{} = event} ->
        Phoenix.PubSub.broadcast(pubsub, @all_topic, {:governance_event, event})

        case event do
          %{"type" => "dialectic" <> _} ->
            Phoenix.PubSub.broadcast(pubsub, @dialectic_topic, {:governance_event, event})

          _ ->
            :ok
        end

      _ ->
        :ignored
    end
  end

  defp send_frame(%{websocket: ws, conn: conn, ref: ref} = state, frame) do
    case Mint.WebSocket.encode(ws, frame) do
      {:ok, ws, data} ->
        case Mint.WebSocket.stream_request_body(conn, ref, data) do
          {:ok, conn} ->
            %{state | websocket: ws, conn: conn}

          {:error, conn, reason} ->
            fail(%{state | websocket: ws, conn: conn}, :unreachable, "Firehose send failed: #{inspect(reason)}")
        end

      {:error, ws, reason} ->
        fail(%{state | websocket: ws}, :unreachable, "Firehose encode failed: #{inspect(reason)}")
    end
  end

  # One failure path: label it, log it (a warning on a change of state, debug
  # while it repeats, so an outage is one line rather than one per attempt),
  # publish the change, and schedule the next attempt on the backoff.
  defp fail(state, status, message) do
    if state.status == status do
      Logger.debug(message)
    else
      Logger.warning(message)
    end

    state |> set_status(status) |> reconnect()
  end

  defp set_status(%{status: status} = state, status), do: state

  defp set_status(state, status) do
    Phoenix.PubSub.broadcast(state.pubsub, @status_topic, {:firehose_status, status})
    %{state | status: status}
  end

  # A failure inside a response batch can be followed by another in the same
  # batch, or by the stream error that carried it; schedule one attempt only.
  defp reconnect(%{conn: nil, reconnect_timer: timer} = state) when timer != nil, do: state

  defp reconnect(state) do
    close_conn(state.conn)
    timer = Process.send_after(self(), :connect, state.backoff_ms)

    %{
      state
      | conn: nil,
        websocket: nil,
        ref: nil,
        connected?: false,
        upgrade_status: nil,
        upgrade_headers: [],
        reconnect_timer: timer,
        backoff_ms: min(state.backoff_ms * 2, state.reconnect_max_ms)
    }
  end

  defp close_conn(nil), do: :ok

  defp close_conn(conn) do
    Mint.HTTP.close(conn)
    :ok
  rescue
    _ -> :ok
  end

  defp schemes(%URI{scheme: "ws"}), do: {:ok, %{http: :http, ws: :ws}}
  defp schemes(%URI{scheme: "wss"}), do: {:ok, %{http: :https, ws: :wss}}
  defp schemes(%URI{scheme: scheme}), do: {:error, {:unsupported_scheme, scheme}}

  defp host(%URI{host: host}) when is_binary(host) and host != "", do: {:ok, host}
  defp host(_uri), do: {:error, :missing_host}

  defp port(%URI{port: port}, _scheme) when is_integer(port), do: port
  defp port(_uri, :ws), do: 80
  defp port(_uri, :wss), do: 443

  defp path(%URI{path: nil, query: nil}), do: "/"
  defp path(%URI{path: "", query: nil}), do: "/"
  defp path(%URI{path: nil, query: query}), do: "/?#{query}"
  defp path(%URI{path: "", query: query}), do: "/?#{query}"
  defp path(%URI{path: path, query: nil}), do: path
  defp path(%URI{path: path, query: query}), do: "#{path}?#{query}"
end
