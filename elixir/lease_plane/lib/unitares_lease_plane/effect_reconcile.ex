defmodule UnitaresLeasePlane.EffectReconcile do
  @moduledoc """
  Crash-recovery reconciliation for governed-effect EXECUTE (§5b). This is the
  safety-critical core: given an orphaned `effects.payloads` row (pre-image
  captured, never committed), decide its fate by comparing the file's CURRENT
  content hash against the recorded hashes — and act ONLY by writing a DB mark.

  The corruption defense, by construction: this NEVER restores file bytes. The
  three outcomes are commit-forward, tombstone, or quarantine — all DB-only — so
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
  with a fake repo and a real temp file.
  """

  alias UnitaresLeasePlane.EffectRepo

  require Logger

  @type outcome :: :committed | :tombstoned | {:quarantined, term()}

  @doc "Reconcile one orphaned payload row. Returns the outcome (also logged)."
  @spec reconcile_payload(map(), module()) :: outcome()
  def reconcile_payload(payload, repo \\ EffectRepo) do
    effect_id = payload.effect_id

    case surface_path(payload) do
      {:ok, path} ->
        dispatch(payload, current_file_sha(path), repo)

      {:error, reason} ->
        # We cannot even locate the surface — cannot prove safety. Quarantine.
        repo.quarantine(effect_id)
        Logger.error("effect_reconcile: #{effect_id} unresolved surface (#{inspect(reason)}) — quarantined")
        {:quarantined, {:surface, reason}}
    end
  end

  defp dispatch(payload, {:ok, current_sha}, repo) do
    effect_id = payload.effect_id

    cond do
      current_sha == payload.payload_sha256 ->
        repo.mark_committed(effect_id)
        Logger.warning("effect_reconcile: #{effect_id} write completed pre-crash — commit-forwarded")
        :committed

      at_pre_image?(payload, current_sha) ->
        repo.tombstone(effect_id)
        Logger.warning("effect_reconcile: #{effect_id} surface at pre-image — tombstoned (retry will re-execute)")
        :tombstoned

      true ->
        repo.quarantine(effect_id)
        Logger.error("effect_reconcile: #{effect_id} surface DIRTY (competing/partial write) — quarantined, NOT touched")
        {:quarantined, :dirty}
    end
  end

  defp dispatch(payload, {:error, reason}, repo) do
    repo.quarantine(payload.effect_id)
    Logger.error("effect_reconcile: #{payload.effect_id} file read failed (#{inspect(reason)}) — quarantined")
    {:quarantined, {:read_error, reason}}
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
