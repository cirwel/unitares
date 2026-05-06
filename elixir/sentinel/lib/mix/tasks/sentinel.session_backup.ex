defmodule Mix.Tasks.Sentinel.SessionBackup do
  @moduledoc """
  Back up the Sentinel governance identity anchor before BEAM cutover.

  ## Usage

      mix sentinel.session_backup
      mix sentinel.session_backup --session-file /path/to/sentinel.json
      mix sentinel.session_backup --backup-file /path/to/sentinel.json.pre-beam
      mix sentinel.session_backup --legacy-session-file /repo/.sentinel_session
      mix sentinel.session_backup --force

  The backup is the binding Surface 5 cutover step:
  `sentinel.json` → `sentinel.json.pre-beam`.
  """

  use Mix.Task

  alias UnitaresSentinel.SessionAnchor

  @shortdoc "Back up Sentinel session anchor before BEAM cutover"

  @impl Mix.Task
  def run(args) do
    opts = parse!(args)

    case SessionAnchor.backup_for_cutover(opts) do
      {:ok, result} ->
        Mix.shell().info("sentinel.session_backup: ok")
        Mix.shell().info("source:     #{result.source}")
        Mix.shell().info("backup:     #{result.backup}")
        Mix.shell().info("agent_uuid: #{result.agent_uuid}")

      {:error, {:backup_exists, path}} ->
        Mix.raise(
          "sentinel.session_backup: backup already exists at #{path}; pass --force to overwrite"
        )

      {:error, reason} ->
        Mix.raise("sentinel.session_backup: #{inspect(reason)}")
    end
  end

  defp parse!(args) do
    {opts, rest, invalid} =
      OptionParser.parse(args,
        strict: [
          session_file: :string,
          backup_file: :string,
          legacy_session_file: :string,
          force: :boolean
        ]
      )

    if rest != [] or invalid != [] do
      Mix.raise("sentinel.session_backup: invalid arguments")
    end

    []
    |> maybe_put(:path, opts[:session_file])
    |> maybe_put(:backup_path, opts[:backup_file])
    |> maybe_put(:legacy_path, opts[:legacy_session_file])
    |> Keyword.put(:force, opts[:force] == true)
  end

  defp maybe_put(opts, _key, nil), do: opts
  defp maybe_put(opts, key, value), do: Keyword.put(opts, key, value)
end
