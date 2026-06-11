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
      {forwarded, failed, failures_by_reason} =
        Enum.reduce(events, {0, 0, %{}}, fn event, {ok_count, fail_count, by_reason} ->
          case Repo.forward_outbox_event(event.event_id) do
            :ok ->
              {ok_count + 1, fail_count, by_reason}

            {:error, :not_found} ->
              {ok_count, fail_count, by_reason}

            {:error, reason} ->
              key = failure_key(reason)

              by_reason =
                Map.update(by_reason, key, {1, event.event_id}, fn {n, sample} ->
                  {n + 1, sample}
                end)

              {ok_count, fail_count + 1, by_reason}
          end
        end)

      # One warning per distinct failure shape per run, not one per row — a
      # stuck batch of 100 rows sharing a single root cause (e.g. a missing
      # audit partition) previously wrote 200 near-identical lines per
      # minute, growing the log by hundreds of MB per week while saying
      # nothing new.
      Enum.each(failures_by_reason, fn {key, {count, sample_event_id}} ->
        Logger.warning(
          "lease_plane audit forward failed for #{count} event(s) " <>
            "(sample #{sample_event_id}): #{key}"
        )
      end)

      {:ok, %{forwarded: forwarded, failed: failed}}
    end
  end

  # Collapse failure terms into a stable grouping key: Postgrex errors keep
  # code + message (connection_id and friends vary per row and would defeat
  # the grouping); everything else falls back to inspect/1.
  defp failure_key(%Postgrex.Error{postgres: %{code: code, message: message}}),
    do: "postgres #{code}: #{message}"

  defp failure_key(reason), do: inspect(reason)

  defp positive_arg(args, string_key, atom_key, default) do
    value = Map.get(args, string_key, Map.get(args, atom_key, default))

    if is_integer(value) and value > 0 do
      value
    else
      default
    end
  end
end
