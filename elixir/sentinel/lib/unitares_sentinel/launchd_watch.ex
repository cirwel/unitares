defmodule UnitaresSentinel.LaunchdWatch do
  @moduledoc """
  Opt-in check for launchd jobs stuck in a crash loop: a job whose last exit
  was non-zero and whose run count keeps climbing between ticks. KeepAlive
  restarts such a job forever, and every liveness surface that asks "is the
  label registered?" says yes.

  Off unless the deployment names which labels to watch:
  `UNITARES_SENTINEL_LAUNCHD_LABEL_PREFIXES` (comma-separated prefixes, empty
  by default) and a `launchctl` on PATH. A bare install, or any non-macOS host,
  starts nothing. Uses only `launchctl list` and `launchctl print` in the
  caller's own GUI domain, which need no privileges.

  Why: a KeepAlive job once crash-looped for 8 days — about 50,000 respawns,
  last exit 1, its port never bound — and appeared on no health surface.

  Needs two samples to see a run count move, so detection is within about two
  ticks (10 minutes at the default 5-minute interval). Re-alerts go through
  `UnitaresSentinel.ReAlert`.
  """

  use GenServer

  require Logger

  alias UnitaresSentinel.{Findings, ReAlert}

  @default_interval_ms 300_000
  @default_initial_delay_ms 90_000
  @default_min_respawns 3
  @max_pending 200
  # At the 3s findings timeout, 5 stalled POSTs cost 15s of a tick.
  @max_deliveries_per_tick 5

  @type job :: %{label: String.t(), status: integer(), pid: integer() | nil}

  # ---- pure parsing / rules -------------------------------------------

  @doc "Parse `launchctl list` output into jobs whose label matches a prefix."
  @spec parse_list(String.t(), [String.t()]) :: [job()]
  def parse_list(output, prefixes) when is_binary(output) do
    output
    |> String.split("\n", trim: true)
    |> Enum.flat_map(fn line ->
      case String.split(line, "\t") do
        [pid, status, label] ->
          with true <- Enum.any?(prefixes, &String.starts_with?(label, &1)),
               {status_int, ""} <- Integer.parse(status) do
            [%{label: label, status: status_int, pid: parse_pid(pid)}]
          else
            _ -> []
          end

        _ ->
          []
      end
    end)
  end

  @doc "Extract `runs = N` from `launchctl print` output, or nil."
  @spec parse_runs(String.t()) :: non_neg_integer() | nil
  def parse_runs(output) when is_binary(output) do
    case Regex.run(~r/^\s*runs = (\d+)\s*$/m, output) do
      [_, n] -> String.to_integer(n)
      _ -> nil
    end
  end

  @doc """
  Given this tick's failing jobs with run counts and the previous tick's run
  counts, return the jobs in a crash loop.
  """
  @spec crash_loops([map()], %{optional(String.t()) => non_neg_integer()}, keyword()) :: [map()]
  def crash_loops(failing, prior_runs, opts \\ []) do
    min_respawns = Keyword.get(opts, :min_respawns, @default_min_respawns)

    Enum.flat_map(failing, fn %{label: label, runs: runs} = job ->
      case Map.get(prior_runs, label) do
        prior when is_integer(prior) and is_integer(runs) and runs - prior >= min_respawns ->
          [Map.put(job, :respawns, runs - prior)]

        _ ->
          []
      end
    end)
  end

  @doc "Finding for one crash-looping job."
  @spec to_finding(map(), map()) :: map()
  def to_finding(%{label: label, status: status, respawns: respawns} = job, emit_info \\ %{}) do
    %{
      type: "launchd_crash_loop",
      violation_class: "BEH",
      severity: "high",
      summary:
        "launchd job #{label} is crash-looping: last exit #{status}, " <>
          "#{respawns} respawns since the previous check (runs=#{Map.get(job, :runs)})",
      fingerprint_extra: [label],
      extra: %{
        label: label,
        last_exit_status: status,
        respawns_since_last_check: respawns,
        runs: Map.get(job, :runs),
        suppressed_since_last: Map.get(emit_info, :suppressed_since_last, 0)
      }
    }
  end

  @doc "Comma-separated prefixes → list, dropping blanks."
  @spec parse_prefixes(String.t() | nil) :: [String.t()]
  def parse_prefixes(nil), do: []

  def parse_prefixes(raw) when is_binary(raw) do
    raw |> String.split(",") |> Enum.map(&String.trim/1) |> Enum.reject(&(&1 == ""))
  end

  @doc "True when this deployment opted in and the host has launchctl."
  @spec enabled?([String.t()]) :: boolean()
  def enabled?(prefixes), do: prefixes != [] and System.find_executable("launchctl") != nil

  # ---- tick --------------------------------------------------------------

  @doc """
  One check. Returns `{posted, prior_runs, realert, pending}`.

  `pending` holds findings whose POST failed; pass it back as `:pending` and
  they are resent first, so a crash loop seen during a governance outage is
  reported even if the job has stopped respawning by the next tick.

  Options: `:prefixes`, `:prior_runs`, `:realert`, `:pending`, `:now_ms`,
  `:list_fun` (`fn -> {:ok, output} | {:error, term} end`), `:print_fun`
  (`fn label -> {:ok, output} | {:error, term} end`), `:min_respawns`,
  `:realert_opts`, `:emit_findings`, `:findings_opts`.
  """
  def tick(opts) do
    now_ms = Keyword.get(opts, :now_ms, System.system_time(:millisecond))
    prefixes = Keyword.get(opts, :prefixes, [])
    realert = opts |> Keyword.get(:realert, ReAlert.new()) |> ReAlert.prune(now_ms)
    prior_runs = Keyword.get(opts, :prior_runs, %{})
    list_fun = Keyword.get(opts, :list_fun, &launchctl_list/0)
    print_fun = Keyword.get(opts, :print_fun, &launchctl_print/1)

    {new_findings, next_runs, realert} =
      case list_fun.() do
        {:ok, output} ->
          failing =
            output
            |> parse_list(prefixes)
            |> Enum.filter(&(&1.status != 0))
            |> Enum.map(fn job ->
              runs =
                case print_fun.(job.label) do
                  {:ok, printed} -> parse_runs(printed)
                  _ -> nil
                end

              Map.put(job, :runs, runs)
            end)

          next_runs =
            for %{label: label, runs: runs} <- failing, is_integer(runs), into: %{}, do: {label, runs}

          {findings, realert} =
            failing
            |> crash_loops(prior_runs,
              min_respawns: Keyword.get(opts, :min_respawns, @default_min_respawns)
            )
            |> Enum.reduce({[], realert}, fn job, {acc, state} ->
              case ReAlert.decide(
                     state,
                     job.label,
                     ReAlert.rank("high"),
                     now_ms,
                     Keyword.get(opts, :realert_opts, [])
                   ) do
                {:emit, info, state} ->
                  finding =
                    job
                    |> to_finding(info)
                    |> Map.put(:change_token, ReAlert.change_token(info, "high"))

                  {[finding | acc], state}

                {:suppress, state} ->
                  {acc, state}
              end
            end)

          {Enum.reverse(findings), next_runs, realert}

        {:error, reason} ->
          Logger.warning("LaunchdWatch: launchctl list failed — #{inspect(reason)}")
          {[], prior_runs, realert}
      end

    # The backoff and the run-count baseline advance whether or not the POST
    # lands: an undelivered finding is queued for resend, not forgotten.
    # Deliveries per tick are capped, new findings first, then the queue.
    {delivered, undelivered} =
      Findings.deliver_bounded(
        new_findings ++ Keyword.get(opts, :pending, []),
        Keyword.get(opts, :max_deliveries, @max_deliveries_per_tick),
        opts,
        "LaunchdWatch"
      )

    {delivered, next_runs, realert, Findings.cap_queue(undelivered, @max_pending)}
  end

  defp launchctl_list do
    case System.cmd("launchctl", ["list"], stderr_to_stdout: true) do
      {output, 0} -> {:ok, output}
      {output, code} -> {:error, {code, String.slice(output, 0, 200)}}
    end
  rescue
    e -> {:error, e}
  end

  defp launchctl_print(label) do
    case System.cmd("launchctl", ["print", "gui/#{uid()}/#{label}"], stderr_to_stdout: true) do
      {output, 0} -> {:ok, output}
      {output, code} -> {:error, {code, String.slice(output, 0, 200)}}
    end
  rescue
    e -> {:error, e}
  end

  defp uid do
    case :persistent_term.get({__MODULE__, :uid}, nil) do
      nil ->
        {out, 0} = System.cmd("id", ["-u"])
        uid = String.trim(out)
        :persistent_term.put({__MODULE__, :uid}, uid)
        uid

      uid ->
        uid
    end
  end

  defp parse_pid("-"), do: nil

  defp parse_pid(pid) do
    case Integer.parse(pid) do
      {n, ""} -> n
      _ -> nil
    end
  end

  # ---- GenServer -------------------------------------------------------

  def start_link(opts \\ []) do
    GenServer.start_link(__MODULE__, opts, name: Keyword.get(opts, :name, __MODULE__))
  end

  @impl true
  def init(opts) do
    state = %{
      opts:
        opts
        |> Keyword.put_new(
          :emit_findings,
          Application.get_env(:unitares_sentinel, :emit_findings, true)
        ),
      interval_ms: Keyword.get(opts, :interval_ms, @default_interval_ms),
      prior_runs: %{},
      realert: ReAlert.new(),
      pending: []
    }

    Process.send_after(self(), :tick, Keyword.get(opts, :initial_delay_ms, @default_initial_delay_ms))
    {:ok, state}
  end

  @impl true
  def handle_info(:tick, state) do
    tick_opts =
      state.opts
      |> Keyword.put(:prior_runs, state.prior_runs)
      |> Keyword.put(:realert, state.realert)
      |> Keyword.put(:pending, state.pending)

    {prior_runs, realert, pending} =
      try do
        {_posted, prior_runs, realert, pending} = tick(tick_opts)
        {prior_runs, realert, pending}
      rescue
        e ->
          Logger.warning("LaunchdWatch: tick raised #{inspect(e)}")
          {state.prior_runs, state.realert, state.pending}
      end

    Process.send_after(self(), :tick, state.interval_ms)
    {:noreply, %{state | prior_runs: prior_runs, realert: realert, pending: pending}}
  end
end
