defmodule UnitaresLeasePlane.Reaper do
  @moduledoc """
  Periodic lease reaper worker.

  The worker is intentionally small and idempotent: it only releases rows that
  are still active and still expired at update time, so an in-flight heartbeat
  that already extended `expires_at` wins.
  """

  require Logger

  alias UnitaresLeasePlane.{LeaseSupervisor, Repo}

  @spec perform(map()) :: {:ok, %{reaped: non_neg_integer()}} | {:error, term()}
  def perform(args \\ %{}) when is_map(args) do
    limit =
      positive_arg(args, "limit", :limit, Application.get_env(:lease_plane, :reaper_limit, 100))

    with {:ok, leases} <- Repo.expired_active_leases(limit) do
      reaped =
        Enum.reduce(leases, 0, fn lease, count ->
          case Repo.release_if_expired(lease.lease_id, release_reason(lease)) do
            :ok ->
              count + 1

            {:error, :not_found} ->
              count

            {:error, reason} ->
              Logger.warning(
                "lease_plane reaper could not release #{lease.lease_id}: #{inspect(reason)}"
              )

              count
          end
        end)

      {:ok, %{reaped: reaped}}
    end
  end

  defp release_reason(%{holder_kind: "remote_heartbeat"}), do: "reaped_remote_ttl"

  defp release_reason(%{holder_kind: "local_beam", lease_id: lease_id}) do
    case LeaseSupervisor.holder_for(lease_id) do
      {:ok, pid} when is_pid(pid) -> "reaped_local_ttl"
      :error -> "reaped_after_supervisor_failed"
    end
  end

  defp positive_arg(args, string_key, atom_key, default) do
    value = Map.get(args, string_key, Map.get(args, atom_key, default))

    if is_integer(value) and value > 0 do
      value
    else
      default
    end
  end
end
