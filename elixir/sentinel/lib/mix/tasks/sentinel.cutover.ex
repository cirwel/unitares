defmodule Mix.Tasks.Sentinel.Cutover do
  @moduledoc """
  Mark BEAM Sentinel as canonical for the cycle-state file.

  ## Usage

      mix sentinel.cutover --to=beam
      mix sentinel.cutover --to=beam --state-file /path/to/.sentinel_state

  The task re-reads `STATE_FILE` and `STATE_FILE.beam`, picks the max
  `forced_release_alarm.last_event_ts`, writes it to `STATE_FILE.beam`, and
  adds `runtime: "beam_canonical"`.

  Stop Python Sentinel before running this task; start BEAM Sentinel after it.
  Launchctl orchestration is intentionally outside this Mix task.
  """

  use Mix.Task

  alias UnitaresSentinel.Cutover

  @shortdoc "Set Sentinel cycle-state runtime to beam_canonical"

  @impl Mix.Task
  def run(args) do
    opts = parse!(args, "beam")
    {:ok, result} = Cutover.cutover_to_beam(opts)

    Mix.shell().info("sentinel.cutover: runtime=#{result.runtime}")
    Mix.shell().info("canonical: #{result.canonical}")
    Mix.shell().info("shadow:    #{result.shadow}")
    Mix.shell().info("cursor:    #{format_cursor(result.cursor)}")
  end

  defp parse!(args, expected) do
    {opts, rest, invalid} = OptionParser.parse(args, strict: [to: :string, state_file: :string])

    if rest != [] or invalid != [] do
      Mix.raise("sentinel.cutover: invalid arguments; use --to=#{expected}")
    end

    if opts[:to] != expected do
      Mix.raise("sentinel.cutover: expected --to=#{expected}")
    end

    case opts[:state_file] do
      nil -> []
      path -> [canonical: path]
    end
  end

  defp format_cursor(nil), do: "<none>"
  defp format_cursor(cursor), do: cursor
end
