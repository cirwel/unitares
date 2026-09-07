defmodule UnitaresLeasePlane.EffectReconcile do
  @moduledoc """
  Crash-recovery reconciliation for governed-effect EXECUTE (§5b). This is the
  safety-critical core: given an orphaned `effects.payloads` row (pre-image
  captured, never committed), decide its fate by comparing the file's CURRENT
  content hash against the recorded hashes — and act ONLY by writing a DB mark.

  The corruption defense, by construction: this NEVER restores file bytes. The
  requested marks are commit-forward, tombstone, or quarantine — all DB-only — so
  a competing writer that acquired the surface after the crash can never be
  clobbered by recovery. (The council BLOCKER on the original "blindly restore
  the pre-image" design is structurally eliminated here.)

  Dispatch:
    * current == payload_sha256         -> the write completed; commit-forward.
    * current == pre_image_sha256, or
      (current is nil AND NOT pre_image_existed) -> surface is at pre-image;
        nothing was committed; tombstone (a same-key retry re-executes, §4).
    * anything else (incl. a read error) -> surface is DIRTY (a competing write
        or a partial write); quarantine — operator-first, retry unsafe. Never
        touch the file.

  `repo` is injectable (default `EffectRepo`) so the dispatch is unit-testable
  with a fake repo and a real temp file. A disposition is reported as resolved
  only after the store acknowledges its mark with `:ok`. Failed or unexpected
  replies return `{:unresolved, ...}`; they never establish persisted recovery.
  """

  alias UnitaresLeasePlane.EffectRepo

  require Logger

  @type resolved_outcome :: :committed | :tombstoned | {:quarantined, term()}
  @type outcome ::
          resolved_outcome()
          | {:unresolved, {:persistence_failed, resolved_outcome(), term()}}

  @doc "Reconcile one orphaned payload row. Returns the outcome (also logged)."
  @spec reconcile_payload(map(), module()) :: outcome()
  def reconcile_payload(payload, repo \\ EffectRepo) do
    intended_outcome =
      case surface_path(payload) do
        {:ok, path} ->
          classify(payload, current_file_sha(path))

        {:error, reason} ->
          # We cannot locate the surface — request quarantine, not a success claim.
          {:quarantined, {:surface, reason}}
      end

    persist_outcome(payload.effect_id, intended_outcome, repo)
  end

  defp classify(payload, {:ok, current_sha}) do
    cond do
      current_sha == payload.payload_sha256 ->
        :committed

      at_pre_image?(payload, current_sha) ->
        :tombstoned

      true ->
        {:quarantined, :dirty}
    end
  end

  defp classify(_payload, {:error, reason}) do
    {:quarantined, {:read_error, reason}}
  end

  defp persist_outcome(effect_id, outcome, repo) do
    result =
      case outcome do
        :committed -> repo.mark_committed(effect_id)
        :tombstoned -> repo.tombstone(effect_id)
        {:quarantined, _} -> repo.quarantine(effect_id)
      end

    case result do
      :ok ->
        message =
          "effect_reconcile: #{effect_id} persisted recovery outcome " <>
            "#{inspect(outcome)}; file untouched"

        case outcome do
          {:quarantined, _} -> Logger.error(message)
          _ -> Logger.warning(message)
        end

        outcome

      {:error, reason} ->
        unresolved(effect_id, outcome, reason)

      other ->
        unresolved(effect_id, outcome, {:unexpected_reply, other})
    end
  end

  defp unresolved(effect_id, intended_outcome, reason) do
    Logger.error(
      "effect_reconcile: #{effect_id} recovery unresolved; persistence not acknowledged " <>
        "for #{inspect(intended_outcome)} (#{inspect(reason)}); file untouched"
    )

    {:unresolved, {:persistence_failed, intended_outcome, reason}}
  end

  defp at_pre_image?(payload, current_sha) do
    cond do
      not is_nil(payload.pre_image_sha256) -> current_sha == payload.pre_image_sha256
      # file did not exist pre-write AND does not exist now -> at pre-image
      is_nil(current_sha) -> payload.pre_image_existed == false
      true -> false
    end
  end

  @doc """
  SHA-256 (lowercase hex) of the file's current bytes, or `{:ok, nil}` when the
  file does not exist (a legitimate pre-image-absent state). `{:error, _}` on any
  other read failure — the caller quarantines.
  """
  @spec current_file_sha(String.t()) :: {:ok, String.t() | nil} | {:error, term()}
  def current_file_sha(path) do
    case File.read(path) do
      {:ok, bytes} -> {:ok, sha256_hex(bytes)}
      {:error, :enoent} -> {:ok, nil}
      {:error, reason} -> {:error, reason}
    end
  end

  @doc false
  def sha256_hex(bytes), do: :crypto.hash(:sha256, bytes) |> Base.encode16(case: :lower)

  # required_leases is JSONB; Postgrex may hand it back as a decoded list or a
  # raw string depending on type config. Take the first lease's surface and
  # strip the file:// scheme to a filesystem path.
  defp surface_path(payload) do
    with {:ok, leases} <- decode_leases(Map.get(payload, :required_leases)),
         [%{} = first | _] <- leases,
         surface when is_binary(surface) <- Map.get(first, "surface") do
      {:ok, strip_file_scheme(surface)}
    else
      _ -> {:error, :no_surface}
    end
  end

  defp decode_leases(list) when is_list(list), do: {:ok, list}

  defp decode_leases(bin) when is_binary(bin) do
    case Jason.decode(bin) do
      {:ok, list} when is_list(list) -> {:ok, list}
      _ -> {:error, :bad_leases}
    end
  end

  defp decode_leases(_), do: {:error, :bad_leases}

  defp strip_file_scheme("file://" <> rest), do: rest
  defp strip_file_scheme(other), do: other
end
