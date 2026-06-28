defmodule UnitaresLeasePlane.DialecticLivenessSupervisor do
  @moduledoc """
  DynamicSupervisor for per-session `DialecticLiveness` processes (Slice 2).
  `ensure_started/1` is idempotent — a session already being watched is a no-op,
  so the reconciler can call it freely on every tick.
  """

  use DynamicSupervisor

  alias UnitaresLeasePlane.DialecticLiveness

  def start_link(init_arg) do
    DynamicSupervisor.start_link(__MODULE__, init_arg, name: __MODULE__)
  end

  @impl true
  def init(_init_arg) do
    DynamicSupervisor.init(strategy: :one_for_one)
  end

  @doc "Start a liveness process for `session_id` unless one already exists."
  def ensure_started(session_id, opts \\ []) when is_binary(session_id) do
    case Registry.lookup(UnitaresLeasePlane.DialecticLivenessRegistry, session_id) do
      [{_pid, _}] ->
        :already_started

      [] ->
        spec = {DialecticLiveness, [{:session_id, session_id} | opts]}

        case DynamicSupervisor.start_child(__MODULE__, spec) do
          {:ok, _pid} -> :started
          {:error, {:already_started, _pid}} -> :already_started
          {:error, reason} -> {:error, reason}
        end
    end
  end

  @doc "Number of sessions currently being watched."
  def watched_count do
    %{active: active} = DynamicSupervisor.count_children(__MODULE__)
    active
  end
end
