defmodule UnitaresLeasePlane.HandoffTimeout do
  @moduledoc """
  Periodic handoff-offer timeout worker.

  Timers inside `HandoffServer` expire offers promptly; this worker is the
  scheduled safety sweep for delayed messages or hot-code reload windows.
  """

  alias UnitaresLeasePlane.HandoffServer

  @spec perform(map()) :: {:ok, %{expired: non_neg_integer()}}
  def perform(_args \\ %{}) do
    {:ok, %{expired: HandoffServer.expire_stale()}}
  end
end
