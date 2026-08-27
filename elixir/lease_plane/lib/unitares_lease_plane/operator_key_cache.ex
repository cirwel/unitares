defmodule UnitaresLeasePlane.OperatorKeyCache do
  @moduledoc "Small TTL cache for federated operator Ed25519 public keys."

  use GenServer

  def start_link(_opts), do: GenServer.start_link(__MODULE__, %{}, name: __MODULE__)

  @spec get(String.t(), String.t()) :: binary() | nil
  def get(issuer, kid) do
    if Process.whereis(__MODULE__) do
      GenServer.call(__MODULE__, {:get, issuer, kid})
    end
  end

  @spec put(String.t(), String.t(), binary()) :: :ok
  def put(issuer, kid, public_key) do
    if Process.whereis(__MODULE__) do
      GenServer.cast(__MODULE__, {:put, issuer, kid, public_key})
    end

    :ok
  end

  @spec evict_issuer(String.t()) :: :ok
  def evict_issuer(issuer) do
    if Process.whereis(__MODULE__) do
      GenServer.cast(__MODULE__, {:evict, issuer})
    end

    :ok
  end

  @impl true
  def init(state), do: {:ok, state}

  @impl true
  def handle_call({:get, issuer, kid}, _from, state) do
    now = System.monotonic_time(:millisecond)

    case Map.get(state, {issuer, kid}) do
      {public_key, expires_at} when expires_at > now ->
        {:reply, public_key, state}

      _ ->
        {:reply, nil, Map.delete(state, {issuer, kid})}
    end
  end

  @impl true
  def handle_cast({:put, issuer, kid, public_key}, state) do
    ttl_ms = Application.get_env(:lease_plane, :operator_key_cache_ttl_ms, 300_000)
    expires_at = System.monotonic_time(:millisecond) + max(ttl_ms, 1_000)
    {:noreply, Map.put(state, {issuer, kid}, {public_key, expires_at})}
  end

  def handle_cast({:evict, issuer}, state) do
    {:noreply,
     Map.reject(state, fn {{cached_issuer, _kid}, _value} -> cached_issuer == issuer end)}
  end
end
