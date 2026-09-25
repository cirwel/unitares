defmodule UnitaresSentinel.LaunchdWatchTest do
  use ExUnit.Case, async: true

  alias UnitaresSentinel.LaunchdWatch

  @min 60 * 1_000

  @list """
  PID\tStatus\tLabel
  -\t0\tcom.example.healthy
  -\t1\tcom.example.gateway
  51440\t-9\tcom.example.server
  -\t1\tcom.other.ignored
  -\t0\tcom.apple.something
  """

  defp print_with_runs(runs), do: "\tstate = not running\n\truns = #{runs}\n\tlast exit code = 1\n"

  test "an empty prefix list disables the check" do
    refute LaunchdWatch.enabled?([])
    assert LaunchdWatch.parse_prefixes(nil) == []
    assert LaunchdWatch.parse_prefixes(" com.a. , ,com.b.") == ["com.a.", "com.b."]
  end

  test "parse_list keeps only labels under the configured prefixes" do
    jobs = LaunchdWatch.parse_list(@list, ["com.example."])
    assert Enum.map(jobs, & &1.label) == ["com.example.healthy", "com.example.gateway", "com.example.server"]
    assert Enum.find(jobs, &(&1.label == "com.example.server")).status == -9
    assert LaunchdWatch.parse_list(@list, []) == []
  end

  test "parse_runs reads the run count" do
    assert LaunchdWatch.parse_runs(print_with_runs(50_619)) == 50_619
    assert LaunchdWatch.parse_runs("state = running\n") == nil
  end

  test "a failing job whose run count climbs between ticks is a crash loop" do
    failing = [%{label: "g", status: 1, runs: 130}, %{label: "s", status: -9, runs: 5}]
    prior = %{"g" => 100, "s" => 5}
    assert [%{label: "g", respawns: 30}] = LaunchdWatch.crash_loops(failing, prior)
    # First sighting has no prior sample: nothing yet.
    assert LaunchdWatch.crash_loops(failing, %{}) == []
  end

  test "two ticks: the second one pages, a third within the hour does not" do
    parent = self()
    counter = :counters.new(1, [])

    # The gateway respawns 22 times between samples; the server does not.
    print_fun = fn
      "com.example.gateway" ->
        :counters.add(counter, 1, 22)
        {:ok, print_with_runs(:counters.get(counter, 1))}

      _ ->
        {:ok, "\tstate = running\n\truns = 5\n"}
    end

    http_post = fn _url, body, _headers, _timeout ->
      send(parent, {:posted, body})
      {:ok, 200, ~s({"success":true})}
    end

    base = [
      prefixes: ["com.example."],
      list_fun: fn -> {:ok, @list} end,
      print_fun: print_fun,
      findings_opts: [agent_id: "sentinel-test", http_post: http_post]
    ]

    {[], runs, realert, []} = LaunchdWatch.tick([now_ms: 0] ++ base)
    refute_receive {:posted, _}

    {[f], runs, realert, []} =
      LaunchdWatch.tick([now_ms: 5 * @min, prior_runs: runs, realert: realert] ++ base)

    assert f.type == "launchd_crash_loop"
    assert_receive {:posted, body}
    assert body["severity"] == "high"
    assert body["label"] == "com.example.gateway"
    assert body["respawns_since_last_check"] == 22

    {[], _runs, _realert, []} =
      LaunchdWatch.tick([now_ms: 10 * @min, prior_runs: runs, realert: realert] ++ base)

    refute_receive {:posted, _}
  end

  test "an undelivered crash-loop alert is queued and delivered after the job stops" do
    parent = self()
    list = "PID\tStatus\tLabel\n-\t1\tcom.example.gateway\n"
    runs = :counters.new(1, [])

    print_fun = fn _ ->
      :counters.add(runs, 1, 10)
      {:ok, "\truns = #{:counters.get(runs, 1)}\n"}
    end

    base = [prefixes: ["com.example."], list_fun: fn -> {:ok, list} end, print_fun: print_fun]
    down = [findings_opts: [http_post: fn _, _, _, _ -> {:error, :econnrefused} end]]

    up = [
      findings_opts: [
        http_post: fn _, body, _, _ ->
          send(parent, {:posted, body})
          {:ok, 200, ~s({"success":true})}
        end
      ]
    ]

    {[], r, a, []} = LaunchdWatch.tick([now_ms: 0] ++ base ++ down)

    {[], r, a, [queued]} =
      LaunchdWatch.tick([now_ms: 5 * @min, prior_runs: r, realert: a] ++ base ++ down)

    # The job has stopped respawning by the next tick; the queued report still lands.
    stopped = [list_fun: fn -> {:ok, "PID\tStatus\tLabel\n"} end]

    {[^queued], _, _, []} =
      LaunchdWatch.tick(
        [now_ms: 10 * @min, prior_runs: r, realert: a, pending: [queued]] ++
          stopped ++ Keyword.delete(base, :list_fun) ++ up
      )

    assert_receive {:posted, %{"label" => "com.example.gateway"}}
  end

  test "launchctl failure posts nothing" do
    assert {[], %{"x" => 1}, %{}, []} =
             LaunchdWatch.tick(
               prefixes: ["com.example."],
               prior_runs: %{"x" => 1},
               realert: %{},
               list_fun: fn -> {:error, :enoent} end
             )
  end
end
