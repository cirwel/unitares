defmodule Mix.Tasks.Sentinel.Rollback do
  @moduledoc """
  Mark Python Sentinel as canonical for the cycle-state file.

  ## Usage

      mix sentinel.rollback --to=python
      mix sentinel.rollback --to=python --state-file /path/to/.sentinel_state

  The task re-reads `STATE_FILE` and `STATE_FILE.beam`, picks the max
  `forced_release_alarm.last_event_ts`, writes that Python-compatible state
  back to `STATE_FILE`, and marks `STATE_FILE.beam` as
  `runtime: "python_canonical"` for rollback forensics.

  Stop BEAM Sentinel before running this task; start Python Sentinel after it.
  """

  use Mix.Task

  alias UnitaresSentinel.Cutover

  @shortdoc "Set Sentinel cycle-state runtime to python_canonical"

  @impl Mix.Task
  def run(args) do
    opts = parse!(args, "python")
    {:ok, result} = Cutover.rollback_to_python(opts)

    Mix.shell().info("sentinel.rollback: runtime=#{result.runtime}")
    Mix.shell().info("canonical: #{result.canonical}")
    Mix.shell().info("shadow:    #{result.shadow}")
    Mix.shell().info("cursor:    #{format_cursor(result.cursor)}")
  end

  defp parse!(args, expected) do
    {opts, rest, invalid} = OptionParser.parse(args, strict: [to: :string, state_file: :string])

    if rest != [] or invalid != [] do
      Mix.raise("sentinel.rollback: invalid arguments; use --to=#{expected}")
    end

    if opts[:to] != expected do
      Mix.raise("sentinel.rollback: expected --to=#{expected}")
    end

    case opts[:state_file] do
      nil -> []
      path -> [canonical: path]
    end
  end

  defp format_cursor(nil), do: "<none>"
  defp format_cursor(cursor), do: cursor
end
