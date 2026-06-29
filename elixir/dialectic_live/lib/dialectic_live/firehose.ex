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
      (none exist server-side yet — #1167 Ask 1 — but the topic is ready for them)

  The doorbell contract for B1: a LiveView subscribes and treats any message as a
  cue to refetch via `DialecticLive.Governance`, exactly as the JS dashboard's
  ws.js does today. True diff-push is a later upgrade once dialectic_* events land.
  """

  use GenServer
  require Logger

  @pubsub DialecticLive.PubSub
  @all_topic "governance:events"
  @dialectic_topic "dialectic:events"

  @default_reconnect_ms 10_000
  @default_connect_timeout_ms 5_000

  defstruct url: nil,
            reconnect_ms: @default_reconnect_ms,
            connect_timeout_ms: @default_connect_timeout_ms,
            conn: nil,
            websocket: nil,
            ref: nil,
            connected?: false,
            upgrade_status: nil,
            upgrade_headers: []

  def all_topic, do: @all_topic
  def dialectic_topic, do: @dialectic_topic

  def child_spec(opts) do
    %{id: __MODULE__, start: {__MODULE__, :start_link, [opts]}}
  end

  def start_link(opts \\ []) do
    GenServer.start_link(__MODULE__, opts, name: __MODULE__)
  end

  def connected?(server \\ __MODULE__), do: GenServer.call(server, :connected?)

  @impl true
  def init(opts) do
    cfg = Application.get_env(:dialectic_live, :governance, [])

    state = %__MODULE__{
      url: Keyword.get(opts, :url) || Keyword.get(cfg, :ws_url),
      reconnect_ms: Keyword.get(opts, :reconnect_ms, @default_reconnect_ms),
      connect_timeout_ms: Keyword.get(opts, :connect_timeout_ms, @default_connect_timeout_ms)
    }

    {:ok, state, {:continue, :connect}}
  end

  @impl true
  def handle_continue(:connect, state), do: connect(state)

  @impl true
  def handle_call(:connected?, _from, state), do: {:reply, state.connected?, state}

  @impl true
  def handle_info(:connect, state), do: connect(state)

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

        Logger.warning("Firehose stream failed: #{inspect(reason)}")
        {:noreply, reconnect(state)}

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
        Logger.warning("Firehose connect failed: #{inspect(reason)}")
        close_conn(conn)
        {:noreply, reconnect(%{state | conn: nil})}

      {:error, reason} ->
        Logger.warning("Firehose config failed: #{inspect(reason)}")
        {:noreply, reconnect(state)}
    end
  end

  defp connect(state) do
    Logger.warning("Firehose has no ws_url configured; not connecting")
    {:noreply, state}
  end

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

  defp handle_response({:done, ref}, %{ref: ref, websocket: nil} = state) do
    case Mint.WebSocket.new(state.conn, ref, state.upgrade_status, state.upgrade_headers) do
      {:ok, conn, websocket} ->
        Logger.info("Firehose connected to #{state.url}")
        %{state | conn: conn, websocket: websocket, connected?: true}

      {:error, conn, reason} ->
        Logger.warning("Firehose upgrade failed: #{inspect(reason)}")
        reconnect(%{state | conn: conn})
    end
  end

  defp handle_response({:done, ref}, %{ref: ref} = state) do
    Logger.warning("Firehose stream closed")
    reconnect(state)
  end

  defp handle_response({:data, ref, data}, %{ref: ref, websocket: ws} = state)
       when not is_nil(ws) do
    case Mint.WebSocket.decode(ws, data) do
      {:ok, ws, frames} ->
        state |> Map.put(:websocket, ws) |> handle_frames(frames)

      {:error, ws, reason} ->
        Logger.warning("Firehose decode failed: #{inspect(reason)}")
        reconnect(%{state | websocket: ws})
    end
  end

  defp handle_response({:error, ref, reason}, %{ref: ref} = state) do
    Logger.warning("Firehose response error: #{inspect(reason)}")
    reconnect(state)
  end

  defp handle_response(_response, state), do: state

  defp handle_frames(state, frames), do: Enum.reduce(frames, state, &handle_frame/2)

  defp handle_frame({:text, message}, state) do
    publish(message)
    state
  end

  defp handle_frame({:ping, payload}, state), do: send_frame(state, {:pong, payload})

  defp handle_frame({:close, code, reason}, state) do
    Logger.warning("Firehose closed by peer: #{inspect({code, reason})}")
    reconnect(state)
  end

  defp handle_frame({:error, reason}, state) do
    Logger.warning("Firehose frame error: #{inspect(reason)}")
    reconnect(state)
  end

  defp handle_frame(_frame, state), do: state

  # --- sink: decode and fan onto PubSub ---

  defp publish(message) when is_binary(message) do
    case Jason.decode(message) do
      {:ok, %{} = event} ->
        Phoenix.PubSub.broadcast(@pubsub, @all_topic, {:governance_event, event})

        case event do
          %{"type" => "dialectic" <> _} ->
            Phoenix.PubSub.broadcast(@pubsub, @dialectic_topic, {:governance_event, event})

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
          {:ok, conn} -> %{state | websocket: ws, conn: conn}
          {:error, conn, reason} ->
            Logger.warning("Firehose send failed: #{inspect(reason)}")
            reconnect(%{state | websocket: ws, conn: conn})
        end

      {:error, ws, reason} ->
        Logger.warning("Firehose encode failed: #{inspect(reason)}")
        reconnect(%{state | websocket: ws})
    end
  end

  defp reconnect(state) do
    close_conn(state.conn)
    Process.send_after(self(), :connect, state.reconnect_ms)

    %{
      state
      | conn: nil,
        websocket: nil,
        ref: nil,
        connected?: false,
        upgrade_status: nil,
        upgrade_headers: []
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
