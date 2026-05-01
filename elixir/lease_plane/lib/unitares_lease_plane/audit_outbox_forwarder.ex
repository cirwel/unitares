defmodule UnitaresLeasePlane.AuditOutboxForwarder do
  @moduledoc """
  Projects lease-plane outbox rows into `audit.tool_usage`.

  `lease_plane.lease_plane_events` remains the canonical outbox. This worker is
  retry-safe at the row level: each event is forwarded inside a transaction and
  then marked with `forwarded_at`.
  """

  require Logger

  alias UnitaresLeasePlane.Repo

  @spec perform(map()) ::
          {:ok, %{forwarded: non_neg_integer(), failed: non_neg_integer()}} | {:error, term()}
  def perform(args \\ %{}) when is_map(args) do
    limit =
      positive_arg(
        args,
        "limit",
        :limit,
        Application.get_env(:lease_plane, :audit_outbox_forward_limit, 100)
      )

    opts =
      case Map.get(args, "surface_id", Map.get(args, :surface_id)) do
        surface_id when is_binary(surface_id) and byte_size(surface_id) > 0 ->
          [surface_id: surface_id]

        _ ->
          []
      end

    with {:ok, events} <- Repo.unforwarded_events(limit, opts) do
      {forwarded, failed} =
        Enum.reduce(events, {0, 0}, fn event, {ok_count, fail_count} ->
          case Repo.forward_outbox_event(event.event_id) do
            :ok ->
              {ok_count + 1, fail_count}

            {:error, :not_found} ->
              {ok_count, fail_count}

            {:error, reason} ->
              Logger.warning(
                "lease_plane audit forward failed for #{event.event_id}: #{inspect(reason)}"
              )

              {ok_count, fail_count + 1}
          end
        end)

      {:ok, %{forwarded: forwarded, failed: failed}}
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
