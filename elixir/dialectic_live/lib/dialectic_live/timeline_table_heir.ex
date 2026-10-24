defmodule DialecticLive.TimelineTableHeir do
  @moduledoc """
  Supervisor-held heir for `DialecticLive.TimelineFeed`'s ETS table.

  The feed owns a protected table and names this process as its heir. If the
  feed crashes, the table, and with it the last window, transfers here, so
  LiveViews keep reading it while the supervisor restarts the feed. The
  restarted feed calls `claim/2` and gets the same table back.

  It holds tables and nothing else; it never writes to them.
  """

  use GenServer

  def child_spec(opts) do
    %{id: Keyword.get(opts, :name, __MODULE__), start: {__MODULE__, :start_link, [opts]}}
  end

  def start_link(opts \\ []) do
    GenServer.start_link(__MODULE__, :ok, name: Keyword.get(opts, :name, __MODULE__))
  end

  @doc """
  Hand `table_name` to the calling process if this heir holds it.
  Returns `{:ok, tid}` or `:none`.
  """
  def claim(heir, table_name), do: GenServer.call(heir, {:claim, table_name})

  @impl true
  def init(:ok), do: {:ok, %{}}

  @impl true
  def handle_call({:claim, table_name}, {pid, _tag}, tables) do
    case Map.pop(tables, table_name) do
      {nil, tables} ->
        {:reply, :none, tables}

      {tid, tables} ->
        :ets.give_away(tid, pid, table_name)
        {:reply, {:ok, tid}, tables}
    end
  end

  @impl true
  def handle_info({:"ETS-TRANSFER", tid, _from, table_name}, tables) do
    {:noreply, Map.put(tables, table_name, tid)}
  end

  def handle_info(_message, tables), do: {:noreply, tables}
end
