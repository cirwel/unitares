defmodule UnitaresSentinel.EISVWebSocket do
  @moduledoc """
  `/ws/eisv` consumer for the BEAM Sentinel.

  The process owns its Mint connection and reconnects after any transport,
  upgrade, decode, or close failure. It deliberately has no application-level
  heartbeat, matching Python Sentinel's `ping_interval=None`; server pings are
  still acknowledged with pong frames.
  """

  use GenServer
  require Logger

  alias UnitaresSentinel.FleetState

  @default_url "ws://localhost:8767/ws/eisv"
  @default_reconnect_ms 10_000
  @default_connect_timeout_ms 5_000

  defstruct url: @default_url,
            reconnect_ms: @default_reconnect_ms,
            connect_timeout_ms: @default_connect_timeout_ms,
            fleet_state: FleetState,
            connect_on_init?: true,
            conn: nil,
            websocket: nil,
            ref: nil,
            connected?: false,
            upgrade_status: nil,
            upgrade_headers: []

  @doc false
  def child_spec(opts) do
    opts = Keyword.put_new(opts, :name, __MODULE__)

    %{
      id: Keyword.get(opts, :name),
      start: {__MODULE__, :start_link, [opts]}
    }
  end

  def start_link(opts \\ []) do
    {name, opts} = Keyword.pop(opts, :name, __MODULE__)
    GenServer.start_link(__MODULE__, opts, name: name)
  end

  def init(opts) do
    state = %__MODULE__{
      url: Keyword.get(opts, :url) || @default_url,
      reconnect_ms: Keyword.get(opts, :reconnect_ms, @default_reconnect_ms),
      connect_timeout_ms: Keyword.get(opts, :connect_timeout_ms, @default_connect_timeout_ms),
      fleet_state: Keyword.get(opts, :fleet_state, FleetState),
      connect_on_init?: Keyword.get(opts, :connect_on_init?, true)
    }

    if state.connect_on_init? do
      {:ok, state, {:continue, :connect}}
    else
      {:ok, state}
    end
  end

  def connected?(server \\ __MODULE__) do
    GenServer.call(server, :connected?)
  end

  @doc """
  Decode one WebSocket text frame and ingest valid JSON object payloads.

  Invalid JSON and non-object JSON are ignored, matching the Python consumer's
  `json.JSONDecodeError` branch and FleetState's map-only event contract.
  """
  def ingest_text(message, fleet_state \\ FleetState)

  def ingest_text(message, fleet_state) when is_binary(message) do
    case Jason.decode(message) do
      {:ok, event} when is_map(event) ->
        FleetState.ingest(fleet_state, event)

      _ ->
        :ignored
    end
  end

  def ingest_text(_message, _fleet_state), do: :ignored

  def handle_continue(:connect, state) do
    connect(state)
  end

  def handle_call(:connected?, _from, state) do
    {:reply, state.connected?, state}
  end

  def handle_info(:connect, state) do
    connect(state)
  end

  def handle_info(message, %{conn: nil} = state) do
    Logger.debug("Sentinel EISV WebSocket ignoring pre-connect message: #{inspect(message)}")
    {:noreply, state}
  end

  def handle_info(message, state) do
    case Mint.WebSocket.stream(state.conn, message) do
      {:ok, conn, responses} ->
        state
        |> Map.put(:conn, conn)
        |> handle_responses(responses)

      {:error, conn, reason, responses} ->
        state =
          state
          |> Map.put(:conn, conn)
          |> handle_responses_without_reply(responses)

        Logger.warning("Sentinel EISV WebSocket stream failed: #{inspect(reason)}")
        {:noreply, reconnect(state)}

      :unknown ->
        {:noreply, state}
    end
  end

  defp connect(state) do
    uri = URI.parse(state.url)

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
        Logger.warning("Sentinel EISV WebSocket connect failed: #{inspect(reason)}")
        close_conn(conn)
        {:noreply, reconnect(%{state | conn: nil})}

      {:error, reason} ->
        Logger.warning("Sentinel EISV WebSocket config failed: #{inspect(reason)}")
        {:noreply, reconnect(state)}
    end
  end

  defp handle_responses(state, responses) do
    state = handle_responses_without_reply(state, responses)
    {:noreply, state}
  end

  defp handle_responses_without_reply(state, responses) do
    Enum.reduce(responses, state, &handle_response/2)
  end

  defp handle_response({:status, ref, status}, %{ref: ref} = state) do
    %{state | upgrade_status: status}
  end

  defp handle_response({:headers, ref, headers}, %{ref: ref} = state) do
    %{state | upgrade_headers: headers}
  end

  defp handle_response({:done, ref}, %{ref: ref, websocket: nil} = state) do
    case Mint.WebSocket.new(state.conn, ref, state.upgrade_status, state.upgrade_headers) do
      {:ok, conn, websocket} ->
        Logger.info("Sentinel EISV WebSocket connected to #{state.url}")
        %{state | conn: conn, websocket: websocket, connected?: true}

      {:error, conn, reason} ->
        Logger.warning("Sentinel EISV WebSocket upgrade failed: #{inspect(reason)}")
        reconnect(%{state | conn: conn})
    end
  end

  defp handle_response({:done, ref}, %{ref: ref} = state) do
    Logger.warning("Sentinel EISV WebSocket stream closed")
    reconnect(state)
  end

  defp handle_response({:data, ref, data}, %{ref: ref, websocket: websocket} = state)
       when not is_nil(websocket) do
    case Mint.WebSocket.decode(websocket, data) do
      {:ok, websocket, frames} ->
        state
        |> Map.put(:websocket, websocket)
        |> handle_frames(frames)

      {:error, websocket, reason} ->
        Logger.warning("Sentinel EISV WebSocket decode failed: #{inspect(reason)}")
        reconnect(%{state | websocket: websocket})
    end
  end

  defp handle_response({:error, ref, reason}, %{ref: ref} = state) do
    Logger.warning("Sentinel EISV WebSocket response error: #{inspect(reason)}")
    reconnect(state)
  end

  defp handle_response(_response, state), do: state

  defp handle_frames(state, frames) do
    Enum.reduce(frames, state, &handle_frame/2)
  end

  defp handle_frame({:text, message}, state) do
    ingest_text(message, state.fleet_state)
    state
  end

  defp handle_frame({:ping, payload}, state) do
    send_frame(state, {:pong, payload})
  end

  defp handle_frame({:close, code, reason}, state) do
    Logger.warning("Sentinel EISV WebSocket closed by peer: #{inspect({code, reason})}")
    reconnect(state)
  end

  defp handle_frame({:error, reason}, state) do
    Logger.warning("Sentinel EISV WebSocket frame error: #{inspect(reason)}")
    reconnect(state)
  end

  defp handle_frame(_frame, state), do: state

  defp send_frame(%{websocket: websocket, conn: conn, ref: ref} = state, frame) do
    case Mint.WebSocket.encode(websocket, frame) do
      {:ok, websocket, data} ->
        send_encoded_frame(%{state | websocket: websocket}, conn, ref, data)

      {:error, websocket, reason} ->
        Logger.warning("Sentinel EISV WebSocket encode failed: #{inspect(reason)}")
        reconnect(%{state | websocket: websocket})
    end
  end

  defp send_encoded_frame(state, conn, ref, data) do
    case Mint.WebSocket.stream_request_body(conn, ref, data) do
      {:ok, conn} ->
        %{state | conn: conn}

      {:error, conn, reason} ->
        Logger.warning("Sentinel EISV WebSocket send failed: #{inspect(reason)}")
        reconnect(%{state | conn: conn})
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
