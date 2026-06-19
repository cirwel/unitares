defmodule Wave3aHandlers.TimeoutDisciplineTest do
  @moduledoc """
  Pins the BEAM listener's own response-time discipline per RFC §3.2.

  The Python proxy at `src/wave3a_beam_proxy.py` enforces a 500ms hard
  timeout on every routed call. The BEAM listener's own work — auth check,
  route match, JSON encode for the response envelope — must complete in
  a small fraction of that budget so PR #5's first real handler (which
  adds a round-trip to the Python probe) has the entire 500ms minus
  network overhead to play with.

  PR #4 ships ONLY the listener skeleton — there's no probe call wired in
  yet — so this test verifies the listener's intrinsic latency only. PR
  #5 will add the round-trip-to-probe timeout test once the dispatch
  surface exists.

  Single-digit milliseconds is the expected ballpark for `/health` (no
  probe, no work). We assert well under 500ms here; tightening to
  single-digit ms would make CI flaky on slow runners and is out of scope
  for this PR.
  """

  use ExUnit.Case, async: false
  import Plug.Test
  import Plug.Conn

  alias Wave3aHandlers.HTTPRouter

  @opts HTTPRouter.init([])
  @bearer "test-bearer-token-do-not-use-in-prod"

  # Well under the 500ms RFC §3.2 budget — the listener's own latency
  # should be a small fraction. 100ms gives headroom for noisy CI while
  # still catching a real regression (e.g., a synchronous probe call
  # accidentally added to the /health path).
  @max_local_latency_ms 100

  setup do
    Application.put_env(:wave3a_handlers, :beam_token, @bearer)
    :ok
  end

  defp authed(conn), do: put_req_header(conn, "authorization", "Bearer #{@bearer}")

  test "GET /health responds well under 500ms (in-process Plug.Test)" do
    # Warm-up call — first invocation includes module loading on a fresh
    # node. The second call measures steady-state.
    _ =
      :get
      |> conn("/health")
      |> HTTPRouter.call(@opts)

    {elapsed_us, resp} =
      :timer.tc(fn ->
        :get
        |> conn("/health")
        |> HTTPRouter.call(@opts)
      end)

    elapsed_ms = div(elapsed_us, 1_000)

    assert resp.status == 200,
           "GET /health must return 200; got #{resp.status} (latency #{elapsed_ms}ms)"

    assert elapsed_ms < @max_local_latency_ms,
           "GET /health latency #{elapsed_ms}ms exceeds local budget #{@max_local_latency_ms}ms — " <>
             "PR #5 will add probe round-trip; PR #4 must stay cheap"
  end

  test "POST /v1/handlers/:tool_name 501 path is also well under 500ms" do
    # The 501 path is the closest analog to a routed dispatch miss — it goes
    # through the full auth → parse → route → encode pipeline minus any probe
    # call. Use a deliberately fake tool name so the test remains stable as
    # real Wave 3a handlers are wired.
    unwired_tool_name = "__unwired_tool_for_latency_test__"
    body = Jason.encode!(%{})

    _ =
      :post
      |> conn("/v1/handlers/#{unwired_tool_name}", body)
      |> put_req_header("content-type", "application/json")
      |> authed()
      |> HTTPRouter.call(@opts)

    {elapsed_us, resp} =
      :timer.tc(fn ->
        :post
        |> conn("/v1/handlers/#{unwired_tool_name}", body)
        |> put_req_header("content-type", "application/json")
        |> authed()
        |> HTTPRouter.call(@opts)
      end)

    elapsed_ms = div(elapsed_us, 1_000)

    assert resp.status == 501

    assert elapsed_ms < @max_local_latency_ms,
           "501 dispatch path latency #{elapsed_ms}ms exceeds local budget " <>
             "#{@max_local_latency_ms}ms (RFC §3.2 budget is 500ms end-to-end including probe)"
  end
end
