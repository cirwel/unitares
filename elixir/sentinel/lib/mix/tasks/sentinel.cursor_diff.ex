defmodule Mix.Tasks.Sentinel.CursorDiff do
  @moduledoc """
  Print the canonical (Python) and shadow (BEAM) Sentinel cursor cursors
  side-by-side, with delta. Operator-facing observability shim for the
  Surface 1 shadow window — see RFC v0.1.2 §C2.

  ## Usage

      mix sentinel.cursor_diff
      UNITARES_SENTINEL_STATE_FILE=/path/to/.sentinel_state mix sentinel.cursor_diff

  Resolves the same path as `UnitaresSentinel.CycleState`: config
  `:unitares_sentinel, :state_file_path` first, then env var.

  Exit code is always 0 — this is a diagnostic, not a gate.
  """

  use Mix.Task

  alias UnitaresSentinel.CycleState

  @shortdoc "Show Python vs BEAM Sentinel cursor positions and delta"

  @impl Mix.Task
  def run(_args) do
    Application.ensure_all_started(:unitares_sentinel)

    # Single source of truth for path resolution lives in CycleState.
    # Catch the raise to convert to Mix.raise/1 for tooling-friendly exit.
    canonical =
      try do
        CycleState.resolve_canonical_path()
      rescue
        e in RuntimeError ->
          Mix.raise("sentinel.cursor_diff: " <> Exception.message(e))
      end

    shadow = canonical <> ".beam"

    canonical_state = read_state(canonical)
    shadow_state = read_state(shadow)

    canonical_cursor = CycleState.get_last_event_ts(canonical_state || %{})
    shadow_cursor = CycleState.get_last_event_ts(shadow_state || %{})

    Mix.shell().info("canonical: #{canonical}")
    Mix.shell().info("  exists:  #{canonical_state != nil}")
    Mix.shell().info("  cursor:  #{format_cursor(canonical_cursor)}")
    Mix.shell().info("")
    Mix.shell().info("shadow:    #{shadow}")
    Mix.shell().info("  exists:  #{shadow_state != nil}")
    Mix.shell().info("  cursor:  #{format_cursor(shadow_cursor)}")
    Mix.shell().info("")
    Mix.shell().info("delta:     #{format_delta(canonical_cursor, shadow_cursor)}")
  end

  defp read_state(path) do
    with {:ok, contents} <- File.read(path),
         {:ok, decoded} when is_map(decoded) <- Jason.decode(contents) do
      decoded
    else
      _ -> nil
    end
  end

  defp format_cursor(nil), do: "<none>"
  defp format_cursor(cursor), do: cursor

  defp format_delta(nil, nil), do: "both empty"
  defp format_delta(c, nil), do: "canonical only (shadow has no cursor): #{c}"
  defp format_delta(nil, s), do: "shadow only (canonical has no cursor): #{s}"

  defp format_delta(c, s) when c == s, do: "in sync"

  defp format_delta(c, s) do
    with {:ok, c_dt, _} <- DateTime.from_iso8601(c),
         {:ok, s_dt, _} <- DateTime.from_iso8601(s) do
      diff_seconds = DateTime.diff(c_dt, s_dt)
      direction = if diff_seconds > 0, do: "canonical leads", else: "shadow leads"
      "#{direction} by #{format_seconds(abs(diff_seconds))}"
    else
      _ -> "lex compare: canonical=#{c} shadow=#{s}"
    end
  end

  defp format_seconds(s) when s < 60, do: "#{s}s"
  defp format_seconds(s) when s < 3600, do: "#{div(s, 60)}m#{rem(s, 60)}s"
  defp format_seconds(s) when s < 86_400, do: "#{div(s, 3600)}h#{div(rem(s, 3600), 60)}m"
  defp format_seconds(s), do: "#{div(s, 86_400)}d#{div(rem(s, 86_400), 3600)}h"
end
