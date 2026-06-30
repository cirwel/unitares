defmodule Wave3aHandlers.Handlers.ListToolsTest do
  @moduledoc """
  ExUnit coverage for the third BEAM-side handler (RFC §5 PR #7).

  Covers (mirrors `get_server_info_test.exs`):

    * Mocked ProbeClient.list_tools/0 returns a fixture envelope → the
      handler passes the `data` payload through verbatim (success path).
    * The probe envelope wrapper (`ok` / `protocol_version`) is NOT
      forwarded into the body — §2.6 parity requires the flat payload.
    * Arguments are accepted and ignored (mirrors `get_server_info`).
    * Probe failure atoms map to the shared typed envelopes
      (`Wave3aHandlers.Handlers.ProbeErrors`).
    * HTTP router dispatch — POST /v1/handlers/list_tools with a mocked
      probe goes end-to-end via Plug.Test and returns the §2.2 envelope.

  Mocking strategy mirrors `get_server_info_test.exs`: `meck` with
  `:passthrough` stubs `ProbeClient.list_tools/0` directly.
  """

  use ExUnit.Case, async: false
  import Plug.Test
  import Plug.Conn

  alias Wave3aHandlers.Handlers.ListTools
  alias Wave3aHandlers.HTTPRouter

  @opts HTTPRouter.init([])
  @bearer "test-bearer-token-do-not-use-in-prod"

  # Fixture mirroring the Python probe's `/v1/probe/list_tools` `data`
  # payload. The probe CALLS `handle_list_tools({})` and surfaces its
  # `success_response` output verbatim, with `server_time` masked by the
  # probe's `mask_timestamps` helper — hence the `<MASKED_TIMESTAMP>` literal
  # and the deterministic `agent_signature: {"uuid": null}` (no bound caller
  # in the probe context).
  @list_tools_data %{
    "success" => true,
    "server_time" => "<MASKED_TIMESTAMP>",
    "agent_signature" => %{"uuid" => nil},
    "tools" => [
      %{"name" => "start_session", "hint" => "Create your identity", "tier" => "essential"},
      %{"name" => "list_tools", "hint" => "List available tools", "tier" => "common"}
    ],
    "total_available" => 24,
    "shown" => 2,
    "more" => "list_tools(lite=false) for all tools with full category details",
    "tip" => "describe_tool(tool_name=...) for parameter details and examples"
  }

  @probe_envelope %{
    "ok" => true,
    "protocol_version" => "wave3a.v1",
    "data" => @list_tools_data
  }

  setup do
    Application.put_env(:wave3a_handlers, :beam_token, @bearer)
    on_exit(fn -> :meck.unload() end)
    :ok
  end

  defp authed(conn), do: put_req_header(conn, "authorization", "Bearer #{@bearer}")
  defp parsed(conn), do: Jason.decode!(conn.resp_body)

  defp stub_probe_ok(envelope \\ @probe_envelope) do
    :meck.new(Wave3aHandlers.ProbeClient, [:passthrough])
    :meck.expect(Wave3aHandlers.ProbeClient, :list_tools, fn -> {:ok, envelope} end)
  end

  defp stub_probe_error(reason) do
    :meck.new(Wave3aHandlers.ProbeClient, [:passthrough])
    :meck.expect(Wave3aHandlers.ProbeClient, :list_tools, fn -> {:error, reason} end)
  end

  describe "ListTools.call/1 success path" do
    test "passes the probe data payload through verbatim" do
      stub_probe_ok()

      assert {:ok, body, 200} = ListTools.call(%{})
      assert body == @list_tools_data
    end

    test "does not forward the probe envelope wrapper" do
      stub_probe_ok()

      assert {:ok, body, 200} = ListTools.call(%{})
      refute Map.has_key?(body, "ok")
      refute Map.has_key?(body, "protocol_version")
      refute Map.has_key?(body, "data")
    end

    test "empty arguments map serves from the probe (default-argument case)" do
      stub_probe_ok()

      assert {:ok, body, 200} = ListTools.call(%{})
      assert body == @list_tools_data
    end

    test "two calls in a row produce identical responses (idempotent)" do
      stub_probe_ok()

      {:ok, body_a, status_a} = ListTools.call(%{})
      {:ok, body_b, status_b} = ListTools.call(%{})

      assert status_a == status_b
      assert body_a == body_b
    end
  end

  describe "ListTools.call/1 non-default-argument delegation (parity guard)" do
    test "non-empty arguments map → 422 delegated_to_python (no probe call)" do
      stub_probe_ok()

      assert {:ok, body, 422} = ListTools.call(%{"tier" => "essential"})
      assert body[:error] == "delegated_to_python"
      assert body[:reason] == "non-default arguments not served on BEAM path"
      assert body[:forwarded] == false
      # The probe must NOT be consulted on the delegation path.
      refute :meck.called(Wave3aHandlers.ProbeClient, :list_tools, [])
    end

    test "any single non-default filter triggers delegation" do
      stub_probe_ok()

      for args <- [
            %{"essential_only" => true},
            %{"lite" => false},
            %{"progressive" => true},
            %{"tier" => "common", "lite" => false}
          ] do
        assert {:ok, %{error: "delegated_to_python"}, 422} = ListTools.call(args)
      end

      refute :meck.called(Wave3aHandlers.ProbeClient, :list_tools, [])
    end
  end

  describe "ListTools.call/1 error paths (shared ProbeErrors mapping)" do
    test "ProbeClient.timeout → 504 with stable error tag" do
      stub_probe_error(:timeout)

      assert {:ok, body, 504} = ListTools.call(%{})
      assert body[:error] == "probe_timeout"
      assert body[:reason] == "Python probe exceeded 500ms budget"
    end

    test "ProbeClient.probe_token_unset → 503" do
      stub_probe_error(:probe_token_unset)

      assert {:ok, body, 503} = ListTools.call(%{})
      assert body[:error] == "service_unavailable"
    end

    test "ProbeClient.connect_error → 502" do
      stub_probe_error(:connect_error)

      assert {:ok, body, 502} = ListTools.call(%{})
      assert body[:error] == "probe_unavailable"
    end

    test "ProbeClient.{:non_200, status} surfaces the upstream status code" do
      stub_probe_error({:non_200, 503})

      assert {:ok, body, 502} = ListTools.call(%{})
      assert body[:error] == "probe_non_200"
      assert body[:probe_status] == 503
    end

    test "ProbeClient.envelope_invalid → 502" do
      stub_probe_error(:envelope_invalid)

      assert {:ok, body, 502} = ListTools.call(%{})
      assert body[:error] == "probe_envelope_invalid"
    end
  end

  describe "HTTP router dispatch" do
    test "POST /v1/handlers/list_tools returns the §2.2 success envelope" do
      stub_probe_ok()

      resp =
        :post
        |> conn("/v1/handlers/list_tools", "{}")
        |> put_req_header("content-type", "application/json")
        |> authed()
        |> HTTPRouter.call(@opts)

      assert resp.status == 200
      body = parsed(resp)

      assert body["ok"] == true
      assert body["protocol_version"] == "wave3a.v1"
      refute Map.has_key?(body, "data")
      assert body["total_available"] == 24
      assert is_list(body["tools"])
    end

    test "probe failure surfaces as ok: false at the probe-mapped status" do
      stub_probe_error(:connect_error)

      resp =
        :post
        |> conn("/v1/handlers/list_tools", "{}")
        |> put_req_header("content-type", "application/json")
        |> authed()
        |> HTTPRouter.call(@opts)

      assert resp.status == 502
      body = parsed(resp)
      assert body["ok"] == false
      assert body["protocol_version"] == "wave3a.v1"
      assert body["error"] == "probe_unavailable"
    end

    test "non-default arguments surface as ok: false / 422 (Python-fallback trigger)" do
      stub_probe_ok()

      resp =
        :post
        |> conn(
          "/v1/handlers/list_tools",
          Jason.encode!(%{"arguments" => %{"tier" => "essential"}})
        )
        |> put_req_header("content-type", "application/json")
        |> authed()
        |> HTTPRouter.call(@opts)

      # 422 is non-2xx → the Python proxy's raise_for_status fires
      # (wave3a_beam_proxy.py::_call_beam) → fallback to the in-process handler.
      assert resp.status == 422
      body = parsed(resp)
      assert body["ok"] == false
      assert body["protocol_version"] == "wave3a.v1"
      assert body["error"] == "delegated_to_python"
    end
  end
end
