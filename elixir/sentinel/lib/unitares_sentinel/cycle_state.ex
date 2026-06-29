defmodule UnitaresSentinel.CycleState do
  @moduledoc """
  Cross-cycle state file (the de-dup fence) for Sentinel-on-BEAM.

  Surface 1 of the Wave 1 RFC. See `docs/proposals/beam-wave-1-sentinel.md`
  v0.1.2 amendment block — that is the binding spec; v0.1.1 §Surface 1
  prose is superseded on every point of conflict.

  ## Path resolution (v0.1.2 §B1)

  Canonical path resolves from `:unitares_sentinel, :state_file_path`
  (Application env), falling back to `UNITARES_SENTINEL_STATE_FILE`
  (system env). Production launchd plist is the source of the env var.
  Shadow path is the canonical path with `.beam` suffix appended,
  written to the same directory.

  ## Boot semantics (v0.1.2 §B2 — max-on-boot)

  `load/1` reads both files when both exist and returns the state with
  the larger `forced_release_alarm.last_event_ts` after ISO-8601 parsing.
  Empty cursors are treated as older than any timestamp.

  ## Cutover (v0.1.2 §B3 — max wins)

  Composes with the boot rule: at cutover, BEAM re-reads both files
  one last time, persists the max to the shadow path, and from then on
  the canonical reader stops touching the Python file. Cutover signal
  is a `runtime` flag in the shadow file itself for forensic clarity.

  ## Save semantics (v0.1.2 §C3, §N1)

  - String-key normalization via `Jason.encode! |> Jason.decode!` —
    atom-keyed input round-trips back as string-keyed regardless of
    the caller's shape.
  - Log-and-continue: `AtomicWrite.write/2` failures are caught,
    logged at `:warning`, and `:ok` is returned. Mirrors Python's
    `save_state` swallow at `agents/sentinel/agent.py:506-508` so
    BEAM does not become more brittle than Python on ENOSPC / RO-fs.
  """

  alias UnitaresSentinel.AtomicWrite

  require Logger

  @forced_release_key "forced_release_alarm"
  @cursor_key "last_event_ts"
  @runtime_key "runtime"
  @runtime_beam_canonical "beam_canonical"

  @type t :: %{String.t() => term()}

  @doc """
  Load the cross-cycle state with max-on-boot semantics.

  Options:
    * `:canonical` — explicit canonical (Python) path, overrides config
    * `:shadow` — explicit shadow (BEAM) path, overrides default
                  (canonical + ".beam")
  """
  @spec load(keyword()) :: t()
  def load(opts \\ []) do
    {canonical, shadow} = resolve_paths(opts)

    shadow_state = read_decode(shadow)

    # Cutover short-circuit (v0.1.2 §B3): once the shadow declares
    # `runtime: "beam_canonical"`, BEAM stops reading the Python file.
    # This honors §B3's "from then on the canonical reader stops touching
    # the Python file" — without it, max-on-boot would let a stale Python
    # cursor silently win after cutover.
    if Map.get(shadow_state, @runtime_key) == @runtime_beam_canonical do
      shadow_state
    else
      canonical_state = read_decode(canonical)
      pick_max(canonical_state, shadow_state)
    end
  end

  @doc """
  Persist `state` to the shadow file, swallowing write errors.

  Always returns `:ok`. Atom-keyed maps are normalized to string keys
  via Jason round-trip before writing.

  Options:
    * `:path` — explicit write target, overrides default shadow path
  """
  @spec save(map(), keyword()) :: :ok
  def save(state, opts \\ []) when is_map(state) do
    path = Keyword.get(opts, :path) || default_shadow_path()

    # Normalize OUTSIDE the try block: caller-side encoding bugs (e.g.,
    # a map containing a PID or a tuple) raise `Protocol.UndefinedError`
    # and MUST propagate. Python's `save_state` only swallows around
    # `atomic_write` — `json.dumps` happens on the same line and a
    # TypeError there would also propagate. Mirror that scope.
    encoded = state |> Jason.encode!() |> Jason.decode!() |> Jason.encode!()

    try do
      :ok = AtomicWrite.write(path, encoded)
    rescue
      e ->
        Logger.warning(
          "UnitaresSentinel.CycleState.save: write failed at #{inspect(path)} — #{inspect(e)}"
        )

        :ok
    end
  end

  @doc """
  Single read accessor for the cursor (mirrors Python's one-site discipline
  at agents/sentinel/agent.py:663).
  """
  @spec get_last_event_ts(t()) :: String.t() | nil
  def get_last_event_ts(state) when is_map(state) do
    state
    |> Map.get(@forced_release_key, %{})
    |> Map.get(@cursor_key)
  end

  @doc """
  Single write accessor for the cursor. Preserves sibling keys under
  `forced_release_alarm`.
  """
  @spec update_last_event_ts(t(), String.t()) :: t()
  def update_last_event_ts(state, cursor) when is_map(state) and is_binary(cursor) do
    inner =
      state
      |> Map.get(@forced_release_key, %{})
      |> Map.put(@cursor_key, cursor)

    Map.put(state, @forced_release_key, inner)
  end

  @doc """
  Read the cutover runtime flag from a state map (`"beam_canonical"`,
  `"python_canonical"`, or `nil` when shadow mode is in effect).
  Per v0.1.2 §B3 cutover protocol.
  """
  @spec get_runtime(t()) :: String.t() | nil
  def get_runtime(state) when is_map(state), do: Map.get(state, @runtime_key)

  @doc false
  @spec compare_cursor(String.t() | nil, String.t() | nil) :: :lt | :eq | :gt
  def compare_cursor(nil, nil), do: :eq
  def compare_cursor(nil, cursor) when is_binary(cursor), do: :lt
  def compare_cursor(cursor, nil) when is_binary(cursor), do: :gt

  def compare_cursor(a, b) when is_binary(a) and is_binary(b) do
    with {:ok, a_dt, _} <- DateTime.from_iso8601(a),
         {:ok, b_dt, _} <- DateTime.from_iso8601(b) do
      DateTime.compare(a_dt, b_dt)
    else
      _ -> lex_compare(a, b)
    end
  end

  @doc """
  Resolve the canonical (Python) STATE_FILE path from config / env var.

  Public so the `mix sentinel.cursor_diff` task and any future Sentinel
  diagnostic can share the resolution discipline — eliminates the drift
  class flagged in the Surface 1 review (reviewer concern: two
  copies of the resolution order).

  Raises if neither `:unitares_sentinel, :state_file_path` (Application env)
  nor `UNITARES_SENTINEL_STATE_FILE` (system env) is set.
  """
  @spec resolve_canonical_path() :: String.t()
  def resolve_canonical_path do
    Application.get_env(:unitares_sentinel, :state_file_path) ||
      System.get_env("UNITARES_SENTINEL_STATE_FILE") ||
      raise """
      UnitaresSentinel.CycleState: STATE_FILE path not configured.
      Set :unitares_sentinel, :state_file_path in config or
      UNITARES_SENTINEL_STATE_FILE in the environment. The launchd
      plist is the source of truth in production.
      """
  end

  # ---- internals ---------------------------------------------------------

  defp resolve_paths(opts) do
    canonical = Keyword.get(opts, :canonical) || resolve_canonical_path()
    shadow = Keyword.get(opts, :shadow) || canonical <> ".beam"
    {canonical, shadow}
  end

  defp default_shadow_path, do: resolve_canonical_path() <> ".beam"

  # Mirrors Python's load_state guard at agents/sentinel/agent.py:494-501:
  # missing file → %{}, decode failure → %{}, non-map decode → %{}.
  defp read_decode(path) do
    with {:ok, contents} <- File.read(path),
         {:ok, decoded} when is_map(decoded) <- Jason.decode(contents) do
      decoded
    else
      _ -> %{}
    end
  end

  # Max-on-boot per v0.1.2 §B2.
  #
  # Single-empty short-circuit FIRST: when only one file decodes to a
  # non-empty map, return that one unconditionally — never run the lex
  # compare against an empty placeholder. The earlier shape (cursor
  # compare with `||""` defaults) silently dropped sibling keys when
  # one side was `%{"forced_release_alarm" => %{}}` and the other was
  # truly absent. Surface 1 review catch.
  defp pick_max(%{} = canonical, shadow) when map_size(canonical) == 0, do: shadow
  defp pick_max(canonical, %{} = shadow) when map_size(shadow) == 0, do: canonical

  defp pick_max(canonical_state, shadow_state) do
    canonical_cursor = get_last_event_ts(canonical_state)
    shadow_cursor = get_last_event_ts(shadow_state)

    if compare_cursor(canonical_cursor, shadow_cursor) in [:gt, :eq] do
      canonical_state
    else
      shadow_state
    end
  end

  defp lex_compare(a, b) do
    cond do
      a > b -> :gt
      a < b -> :lt
      true -> :eq
    end
  end
end
