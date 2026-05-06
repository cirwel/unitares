defmodule UnitaresSentinel.SessionAnchor do
  @moduledoc """
  Python-compatible Sentinel governance identity anchor.

  Surface 5 of Wave 1. Python `SentinelAgent` passes
  `~/.unitares/anchors/sentinel.json` to `GovernanceAgent`, whose live
  contract is:

    * migrate from legacy `.sentinel_session` if the host anchor is missing;
    * read `agent_uuid`, optional `continuity_token`, and optional
      `client_session_id`;
    * preserve the JSON object shape so rollback to Python can resume.

  This module is intentionally conservative: it can read, migrate, validate,
  derive future REST resume payloads, and create the required `.pre-beam`
  cutover backup. It does not mint identities.
  """

  alias UnitaresSentinel.AtomicWrite

  @agent_uuid_key "agent_uuid"
  @continuity_token_key "continuity_token"
  @client_session_id_key "client_session_id"
  @default_agent_name "Sentinel"

  @type t :: %{String.t() => term()}
  @type load_error ::
          :missing_anchor
          | :empty_anchor
          | {:invalid_json, term()}
          | :anchor_not_object
          | :missing_agent_uuid
          | {:invalid_field, String.t()}

  @doc """
  Load the Sentinel session anchor, migrating the legacy project-local anchor
  first when needed.

  Options:
    * `:path` — explicit `sentinel.json` path
    * `:legacy_path` — explicit legacy `.sentinel_session` path
  """
  @spec load(keyword()) :: {:ok, t()} | {:error, load_error()}
  def load(opts \\ []) do
    path = Keyword.get(opts, :path) || resolve_path()
    legacy_path = Keyword.get(opts, :legacy_path) || resolve_legacy_path()

    maybe_migrate_legacy(path, legacy_path)

    path
    |> read_json_object()
    |> validate()
  end

  @doc """
  Same as `load/1`, but raises on invalid or missing anchors.
  """
  @spec load!(keyword()) :: t()
  def load!(opts \\ []) do
    case load(opts) do
      {:ok, anchor} ->
        anchor

      {:error, reason} ->
        raise ArgumentError, "invalid Sentinel session anchor: #{inspect(reason)}"
    end
  end

  @doc """
  Build the future REST identity resume payload from a loaded anchor.

  Optional fields are included only when present, matching Python
  `_ensure_identity/1` and `GovernanceClient` injection behavior.
  """
  @spec resume_payload(t(), keyword()) :: map()
  def resume_payload(anchor, opts \\ []) when is_map(anchor) do
    %{
      "name" => Keyword.get(opts, :name, @default_agent_name),
      "agent_uuid" => Map.fetch!(anchor, @agent_uuid_key),
      "resume" => true
    }
    |> put_optional(@continuity_token_key, Map.get(anchor, @continuity_token_key))
    |> put_optional(@client_session_id_key, Map.get(anchor, @client_session_id_key))
  end

  @doc """
  Create the binding pre-BEAM backup for direct identity cutover.

  Defaults to `<sentinel.json>.pre-beam`. The source anchor is validated
  before copy, and the destination is written atomically with mode 0600.
  """
  @spec backup_for_cutover(keyword()) ::
          {:ok, %{source: Path.t(), backup: Path.t(), agent_uuid: String.t()}}
          | {:error, term()}
  def backup_for_cutover(opts \\ []) do
    path = Keyword.get(opts, :path) || resolve_path()
    backup_path = Keyword.get(opts, :backup_path) || path <> ".pre-beam"
    force? = Keyword.get(opts, :force, false)

    with {:ok, anchor} <- load(opts),
         :ok <- ensure_backup_target(backup_path, force?),
         {:ok, bytes} <- File.read(path),
         :ok <- AtomicWrite.write(backup_path, bytes) do
      {:ok, %{source: path, backup: backup_path, agent_uuid: Map.fetch!(anchor, @agent_uuid_key)}}
    else
      {:error, _} = err -> err
      other -> {:error, other}
    end
  end

  @doc """
  Resolve the host-scoped Sentinel session anchor.

  Config/env order:
    1. `:unitares_sentinel, :session_file_path`
    2. `UNITARES_SENTINEL_SESSION_FILE`
    3. `<home>/.unitares/anchors/sentinel.json`

  Uses `System.user_home/0` instead of `Path.expand("~")` so launchd
  environments without `HOME` still resolve through Erlang's user-home
  lookup when available.
  """
  @spec resolve_path() :: Path.t()
  def resolve_path do
    Application.get_env(:unitares_sentinel, :session_file_path) ||
      System.get_env("UNITARES_SENTINEL_SESSION_FILE") ||
      Path.join([user_home!(), ".unitares", "anchors", "sentinel.json"])
  end

  @doc """
  Resolve Python Sentinel's legacy project-local `.sentinel_session` path.
  """
  @spec resolve_legacy_path() :: Path.t()
  def resolve_legacy_path do
    Application.get_env(:unitares_sentinel, :legacy_session_file_path) ||
      System.get_env("UNITARES_SENTINEL_LEGACY_SESSION_FILE") ||
      Path.expand(Path.join([__DIR__, "..", "..", "..", "..", ".sentinel_session"]))
  end

  defp maybe_migrate_legacy(path, legacy_path) do
    if not File.exists?(path) and File.exists?(legacy_path) do
      case read_json_object(legacy_path) do
        {:ok, legacy} when map_size(legacy) > 0 ->
          File.mkdir_p!(Path.dirname(path))
          AtomicWrite.write(path, Jason.encode!(legacy))

        _ ->
          :ok
      end
    end

    :ok
  rescue
    _ -> :ok
  end

  defp read_json_object(path) do
    case File.read(path) do
      {:ok, ""} ->
        {:error, :empty_anchor}

      {:ok, contents} ->
        case Jason.decode(contents) do
          {:ok, %{} = decoded} -> {:ok, decoded}
          {:ok, _} -> {:error, :anchor_not_object}
          {:error, reason} -> {:error, {:invalid_json, reason}}
        end

      {:error, :enoent} ->
        {:error, :missing_anchor}

      {:error, reason} ->
        {:error, reason}
    end
  end

  defp validate({:error, reason}), do: {:error, reason}

  defp validate({:ok, anchor}) do
    cond do
      not non_empty_binary?(Map.get(anchor, @agent_uuid_key)) ->
        {:error, :missing_agent_uuid}

      invalid_optional?(anchor, @continuity_token_key) ->
        {:error, {:invalid_field, @continuity_token_key}}

      invalid_optional?(anchor, @client_session_id_key) ->
        {:error, {:invalid_field, @client_session_id_key}}

      true ->
        {:ok, anchor}
    end
  end

  defp ensure_backup_target(path, false) do
    if File.exists?(path), do: {:error, {:backup_exists, path}}, else: :ok
  end

  defp ensure_backup_target(_path, true), do: :ok

  defp put_optional(payload, _key, nil), do: payload
  defp put_optional(payload, _key, ""), do: payload
  defp put_optional(payload, key, value) when is_binary(value), do: Map.put(payload, key, value)

  defp invalid_optional?(anchor, key) do
    case Map.get(anchor, key) do
      nil -> false
      value -> not non_empty_binary?(value)
    end
  end

  defp non_empty_binary?(value), do: is_binary(value) and String.trim(value) != ""

  defp user_home! do
    case System.user_home() do
      home when is_binary(home) and home != "" -> home
      _ -> raise "could not resolve user home for Sentinel session anchor"
    end
  end
end
