defmodule UnitaresLeasePlane.FileWriteExecutor do
  @moduledoc """
  File-write executor with durable custody admission. Live writes reserve the
  exact acquired lease before reading the pre-image. The reservation covers
  commit and compensation and survives DB disconnects and process crashes.
  Release, handoff and expiry cannot admit a successor until it is retired.
  Unknown outcomes retain custody for operator recovery; see
  `docs/operations/effect-custody-recheck.md`.

  Both dispatch and commit flags default off. Commit-off is a validating
  dry-run with no reservation or filesystem mutation.
  """

  @behaviour UnitaresLeasePlane.EffectExecutor

  alias UnitaresLeasePlane.{Canonicalize, Repo}

  # Per-class payload ceiling for file_write; global hard backstop applied above.
  @default_max_bytes 1_048_576

  @impl true
  def reversible?, do: true

  @doc """
  Resolve a payload's bytes + content hash through the SAME path apply_effect
  uses. The dispatch calls this to populate the durable effects.payloads row
  BEFORE committing — record_pre_image is an UPDATE and needs the row to exist.
  """
  def resolved_payload(payload) do
    case resolve_content(payload) do
      {:ok, bytes} -> {:ok, bytes, sha256_hex(bytes)}
      {:error, _} = err -> err
    end
  end

  @doc false
  def resolved_target_surface(payload, leases) do
    with raw when is_binary(raw) <- Map.get(payload, "path") || {:error, :path_required},
         {:ok, canonical} <- Canonicalize.canonicalize("file://" <> raw),
         true <- canonical in lease_surfaces(leases) || {:error, :surface_path_mismatch} do
      {:ok, canonical}
    else
      {:error, reason} -> {:error, reason}
      _ -> {:error, :surface_path_mismatch}
    end
  end

  @impl true
  def apply_effect(effect_id, payload, leases) do
    with {:ok, path} <- resolve_path(payload, leases),
         {:ok, content} <- resolve_content(payload),
         :ok <- check_ceiling(content) do
      if commit_enabled?() do
        with :ok <- Repo.begin_effect_custody(effect_id, leases) do
          {pre_sha, pre_bytes, existed?} = read_pre_image(path)
          result = commit(effect_id, path, content, pre_sha, pre_bytes, existed?)
          finish_custody(effect_id, leases, result)
        else
          {:error, reason} -> {:rejected, reason}
        end
      else
        {pre_sha, _pre_bytes, existed?} = read_pre_image(path)

        {:committed,
         %{
           dry_run: true,
           path: path,
           would_write_bytes: byte_size(content),
           payload_sha256: sha256_hex(content),
           pre_image_sha256: pre_sha,
           pre_image_existed: existed?
         }}
      end
    else
      {:error, reason} -> {:rejected, reason}
    end
  end

  # Deliberately no rescue/after: a crash or interrupted filesystem call leaves
  # a durable fence. A completed compensation is safe to release; a dirty
  # surface or unacknowledged commit stays fenced for operator reconciliation.
  defp finish_custody(_id, _leases, {:rejected, :rollback_failed} = result), do: result
  defp finish_custody(_id, _leases, {:committed, %{mark_deferred: true}} = result), do: result

  defp finish_custody(id, leases, result) do
    case Repo.finish_effect_custody(id, leases) do
      :ok ->
        result

      {:error, _} ->
        case result do
          {:committed, meta} -> {:committed, Map.put(meta, :custody_retained, true)}
          _ -> result
        end
    end
  end

  # --- live commit + in-process compensation (§5b) ---------------------------
  # Order: persist the pre-image FIRST (so crash recovery can reconcile), then
  # write, then mark committed. On a write failure, restore the pre-image while
  # we still hold the lease, then tombstone (retry re-executes); if the restore
  # itself fails, quarantine (surface is dirty, operator-first).

  defp commit(effect_id, path, content, pre_sha, pre_bytes, existed?) do
    case repo().record_pre_image(effect_id, pre_sha, pre_bytes, existed?) do
      :ok -> do_write(effect_id, path, content, pre_bytes, existed?)
      :already -> {:rejected, {:persist_failed, :pre_image_not_fresh}}
      {:error, reason} -> {:rejected, {:persist_failed, reason}}
    end
  end

  defp do_write(effect_id, path, content, pre_bytes, existed?) do
    case file_ops().write(path, content) do
      :ok ->
        case repo().mark_committed(effect_id) do
          :ok ->
            {:committed, committed_meta(path, content)}

          {:error, _reason} ->
            # The write IS durable. NEVER compensate a successful commit — that
            # would undo a real change. Leave rollback_state 'pending'; crash
            # recovery will commit-forward (hash == payload_sha256). Report
            # success with the deferred mark noted.
            {:committed, Map.put(committed_meta(path, content), :mark_deferred, true)}
        end

      {:error, write_reason} ->
        compensate(effect_id, path, pre_bytes, existed?, write_reason)
    end
  end

  defp compensate(effect_id, path, pre_bytes, existed?, write_reason) do
    restore =
      if existed?, do: file_ops().write(path, pre_bytes), else: normalize_rm(file_ops().rm(path))

    case restore do
      :ok ->
        repo().tombstone(effect_id)
        {:rejected, {:committed_failed_rolled_back, write_reason}}

      {:error, _restore_reason} ->
        repo().quarantine(effect_id)
        {:rejected, :rollback_failed}
    end
  end

  # rm of an already-absent file means "nothing we created to undo" -> success.
  defp normalize_rm({:error, :enoent}), do: :ok
  defp normalize_rm(other), do: other

  defp committed_meta(path, content),
    do: %{path: path, bytes_written: byte_size(content), payload_sha256: sha256_hex(content)}

  # --- validation (the path the dry-run proves) ------------------------------

  defp resolve_path(payload, leases) do
    with {:ok, canonical} <- resolved_target_surface(payload, leases) do
      {:ok, strip_scheme(canonical)}
    end
  end

  defp lease_surfaces(leases) do
    for l <- leases, s = surface_of(l), is_binary(s), do: s
  end

  defp surface_of(%{"surface" => s}), do: s
  defp surface_of(%{surface: s}), do: s
  defp surface_of(%{surface_id: s}), do: s
  defp surface_of(_), do: nil

  defp resolve_content(%{"content" => c} = payload) when is_binary(c) do
    case Map.get(payload, "encoding") do
      "base64" ->
        case Base.decode64(c) do
          {:ok, bytes} -> {:ok, bytes}
          :error -> {:error, :bad_base64}
        end

      _ ->
        {:ok, c}
    end
  end

  defp resolve_content(_), do: {:error, :content_required}

  defp check_ceiling(content) do
    if byte_size(content) <= max_bytes(), do: :ok, else: {:error, :payload_too_large}
  end

  defp read_pre_image(path) do
    case file_ops().read(path) do
      {:ok, bytes} -> {sha256_hex(bytes), bytes, true}
      {:error, :enoent} -> {nil, nil, false}
      # exists but unreadable: record honestly; no bytes to restore.
      {:error, _} -> {nil, nil, true}
    end
  end

  # Injectable seams (default real File / EffectRepo) so the live commit AND
  # every step of the compensation can be fault-injected in tests — the dialectic
  # precondition for the live-write path.
  defp file_ops,
    do: Application.get_env(:lease_plane, :effect_file_ops, UnitaresLeasePlane.EffectFileOps)

  defp repo, do: Application.get_env(:lease_plane, :effect_repo, UnitaresLeasePlane.EffectRepo)

  defp commit_enabled?,
    do: Application.get_env(:lease_plane, :execute_file_write_commit_enabled, false) == true

  defp max_bytes,
    do: Application.get_env(:lease_plane, :file_write_payload_max_bytes, @default_max_bytes)

  defp strip_scheme("file://" <> rest), do: rest
  defp strip_scheme(other), do: other

  defp sha256_hex(bytes), do: :crypto.hash(:sha256, bytes) |> Base.encode16(case: :lower)
end
