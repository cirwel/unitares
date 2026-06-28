defmodule UnitaresLeasePlane.FileWriteExecutor do
  @moduledoc """
  Executor for the `file_write` governed effect — the first REVERSIBLE execute
  surface (§5a/§10). This slice is DRY-RUN ONLY: it proves the full validation
  path (canonicalize the surface, confirm it matches a held lease, check the
  payload ceiling, read the would-be rollback pre-image) and returns a dry-run
  result WITHOUT writing a byte and WITHOUT any durable side effect.

  Governed by the dialectic review of slice 2 (resolved 2026-06-28, action
  `resume` with conditions): "dry-run-first — the FileWriteExecutor lands first
  in a mode that captures the pre-image and returns a dry-run result WITHOUT
  File.write, proving the lease+veto+pre-image path before any real byte is
  committed." The live write + the in-process compensation path (with
  fault-injection at every step of the live rollback) are the NEXT slice and are
  a hard pre-flag-on gate per that resolution.

  Two-flag fail-safe (defense in depth): `:execute_file_write_enabled` gates the
  dispatch; `:execute_file_write_commit_enabled` (default false) gates whether a
  real `File.write` is allowed. With commit disabled, `apply_effect/3` is a pure
  validating dry-run even if the dispatch flag is on.
  """

  @behaviour UnitaresLeasePlane.EffectExecutor

  alias UnitaresLeasePlane.Canonicalize

  # Per-class payload ceiling for file_write; global hard backstop applied above.
  @default_max_bytes 1_048_576

  @impl true
  def reversible?, do: true

  @impl true
  def apply_effect(_effect_id, payload, leases) do
    with {:ok, path} <- resolve_path(payload, leases),
         {:ok, content} <- resolve_content(payload),
         :ok <- check_ceiling(content) do
      {pre_sha, existed?} = read_pre_image(path)

      if commit_enabled?() do
        # Slice 2b: real write + mark_committed + in-process compensation, gated
        # on the fault-injection tests the dialectic made a hard precondition.
        {:rejected, :commit_not_enabled}
      else
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

  # --- validation (the path the dry-run proves) ------------------------------

  defp resolve_path(payload, leases) do
    with raw when is_binary(raw) <- Map.get(payload, "path") || {:error, :path_required},
         {:ok, canonical} <- Canonicalize.canonicalize("file://" <> raw),
         true <- canonical in lease_surfaces(leases) || {:error, :surface_path_mismatch} do
      {:ok, strip_scheme(canonical)}
    else
      {:error, reason} -> {:error, reason}
      _ -> {:error, :surface_path_mismatch}
    end
  end

  defp lease_surfaces(leases) do
    for l <- leases, s = surface_of(l), is_binary(s), do: s
  end

  defp surface_of(%{"surface" => s}), do: s
  defp surface_of(%{surface: s}), do: s
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
    case File.read(path) do
      {:ok, bytes} -> {sha256_hex(bytes), true}
      {:error, :enoent} -> {nil, false}
      # a read error is surfaced as "exists but unknown" — the dry-run records it
      # honestly; the commit slice will reject on an unreadable target.
      {:error, _} -> {nil, true}
    end
  end

  defp commit_enabled?,
    do: Application.get_env(:lease_plane, :execute_file_write_commit_enabled, false) == true

  defp max_bytes,
    do: Application.get_env(:lease_plane, :file_write_payload_max_bytes, @default_max_bytes)

  defp strip_scheme("file://" <> rest), do: rest
  defp strip_scheme(other), do: other

  defp sha256_hex(bytes), do: :crypto.hash(:sha256, bytes) |> Base.encode16(case: :lower)
end
