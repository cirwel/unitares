defmodule UnitaresLeasePlane.OperatorKeyCache do
  @moduledoc "Issuer-scoped TTL cache for operator Ed25519 public-key sets."

  use GenServer

  def start_link(_opts), do: GenServer.start_link(__MODULE__, %{}, name: __MODULE__)

  @spec get(String.t(), String.t()) :: binary() | :missing | nil
  def get(issuer, kid) do
    if Process.whereis(__MODULE__) do
      GenServer.call(__MODULE__, {:get, issuer, kid})
    end
  end

  @spec put(String.t(), %{optional(String.t()) => binary()}) :: :ok
  def put(issuer, keys) when is_binary(issuer) and is_map(keys) do
    if Process.whereis(__MODULE__) do
      GenServer.call(__MODULE__, {:put, issuer, keys})
    end

    :ok
  end

  @spec evict_issuer(String.t()) :: :ok
  def evict_issuer(issuer) do
    if Process.whereis(__MODULE__) do
      GenServer.call(__MODULE__, {:evict, issuer})
    end

    :ok
  end

  @impl true
  def init(state), do: {:ok, state}

  @impl true
  def handle_call({:get, issuer, kid}, _from, state) do
    now = System.monotonic_time(:millisecond)

    case Map.get(state, issuer) do
      {keys, expires_at} when expires_at > now ->
        {:reply, Map.get(keys, kid, :missing), state}

      _ ->
        {:reply, nil, Map.delete(state, issuer)}
    end
  end

  @impl true
  def handle_call({:put, issuer, keys}, _from, state) do
    ttl_ms = Application.get_env(:lease_plane, :operator_key_cache_ttl_ms, 300_000)
    expires_at = System.monotonic_time(:millisecond) + max(ttl_ms, 1_000)
    {:reply, :ok, Map.put(state, issuer, {keys, expires_at})}
  end

  def handle_call({:evict, issuer}, _from, state) do
    {:reply, :ok, Map.delete(state, issuer)}
  end
end
