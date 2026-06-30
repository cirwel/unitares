defmodule Wave3aHandlers.Handlers.DescribeToolTest do
  @moduledoc """
  ExUnit coverage for the fourth (final) BEAM-side handler (RFC §5 PR #8),
  completing the §1.1 set (4/4).

  `describe_tool` is the FIRST arg-reading handler, so coverage extends the
  `get_server_info` template with argument-handling cases:

    * `tool_name` is pulled from `arguments` and forwarded to
      `ProbeClient.describe_tool/1` (verified via the captured arg).
    * Probe `data` payload is passed through verbatim (success path), and the
      semantic-error payload (`success: false`) also rides through `data`.
    * Missing / empty `tool_name` short-circuits with a 400
      `invalid_arguments` envelope (no probe call) — the Python proxy falls
      back to the in-process handler's canonical error.
    * Probe failure atoms map to the shared typed envelopes
      (`Wave3aHandlers.Handlers.ProbeErrors`).
    * HTTP router dispatch — POST /v1/handlers/describe_tool with the
      proxy-shaped body (`{"arguments": {"tool_name": ...}}`).

  Mocking strategy mirrors `get_server_info_test.exs`: `meck` with
  `:passthrough` stubs `ProbeClient.describe_tool/1` directly.
  """

  use ExUnit.Case, async: false
  import Plug.Test
  import Plug.Conn

  alias Wave3aHandlers.Handlers.DescribeTool
  alias Wave3aHandlers.HTTPRouter

  @opts HTTPRouter.init([])
  @bearer "test-bearer-token-do-not-use-in-prod"

  # Fixture mirroring the Python probe's `/v1/probe/describe_tool` `data`
  # payload — the masked `handle_describe_tool` lite output.
  @describe_data %{
    "success" => true,
    "server_time" => "<MASKED_TIMESTAMP>",
    "agent_signature" => %{"uuid" => nil},
    "tool" => "list_tools",
    "description" => "List all available governance tools",
    "tier" => "common",
    "operation" => "read",
    "parameters" => ["tier: string", "lite: boolean"],
    "note" => "Lite mode - use describe_tool(tool_name=..., lite=false) for full schema"
  }

  @probe_envelope %{
    "ok" => true,
    "protocol_version" => "wave3a.v1",
    "data" => @describe_data
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
    :meck.expect(Wave3aHandlers.ProbeClient, :describe_tool, fn _tool_name -> {:ok, envelope} end)
  end

  # Stub that echoes the forwarded tool_name back into the payload so the
  # test can assert the argument actually reached ProbeClient.describe_tool/1.
  defp stub_probe_echo do
    :meck.new(Wave3aHandlers.ProbeClient, [:passthrough])

    :meck.expect(Wave3aHandlers.ProbeClient, :describe_tool, fn tool_name ->
      {:ok, %{"ok" => true, "protocol_version" => "wave3a.v1", "data" => %{"tool" => tool_name}}}
    end)
  end

  defp stub_probe_error(reason) do
    :meck.new(Wave3aHandlers.ProbeClient, [:passthrough])

    :meck.expect(Wave3aHandlers.ProbeClient, :describe_tool, fn _tool_name -> {:error, reason} end)
  end

  describe "DescribeTool.call/1 argument handling" do
    test "pulls tool_name from arguments and forwards it to the probe" do
      stub_probe_echo()

      assert {:ok, body, 200} = DescribeTool.call(%{"tool_name" => "health_check"})
      assert body == %{"tool" => "health_check"}
    end

    test "missing tool_name short-circuits with a 400 invalid_arguments envelope" do
      stub_probe_ok()

      assert {:ok, body, 400} = DescribeTool.call(%{})
      assert body[:error] == "invalid_arguments"
      assert body[:reason] == "tool_name is required"
      # No probe call should have been made on the short-circuit path.
      refute :meck.called(Wave3aHandlers.ProbeClient, :describe_tool, :_)
    end

    test "empty tool_name short-circuits with a 400 (no probe call)" do
      stub_probe_ok()

      assert {:ok, body, 400} = DescribeTool.call(%{"tool_name" => ""})
      assert body[:error] == "invalid_arguments"
      refute :meck.called(Wave3aHandlers.ProbeClient, :describe_tool, :_)
    end

    test "non-binary tool_name short-circuits with a 400" do
      stub_probe_ok()

      assert {:ok, _body, 400} = DescribeTool.call(%{"tool_name" => 123})
    end
  end

  describe "DescribeTool.call/1 non-tool_name-argument delegation (parity guard)" do
    test "an extra key alongside tool_name → 422 delegated_to_python (no probe call)" do
      stub_probe_ok()

      assert {:ok, body, 422} =
               DescribeTool.call(%{"tool_name" => "list_tools", "lite" => false})

      assert body[:error] == "delegated_to_python"
      assert body[:forwarded] == false
      # The probe must NOT be consulted when an unforwarded arg is present.
      refute :meck.called(Wave3aHandlers.ProbeClient, :describe_tool, :_)
    end

    test "include_schema / include_full_description also delegate" do
      stub_probe_ok()

      for args <- [
            %{"tool_name" => "list_tools", "include_schema" => true},
            %{"tool_name" => "list_tools", "include_full_description" => true}
          ] do
        assert {:ok, %{error: "delegated_to_python"}, 422} = DescribeTool.call(args)
      end

      refute :meck.called(Wave3aHandlers.ProbeClient, :describe_tool, :_)
    end

    test "an extra key WITHOUT tool_name still delegates (422, not 400)" do
      stub_probe_ok()

      # The unforwarded-arg guard runs before the tool_name check, so the
      # caller is delegated to Python (which owns both the filter handling and
      # the canonical missing-tool_name error) rather than served a 400 here.
      assert {:ok, %{error: "delegated_to_python"}, 422} =
               DescribeTool.call(%{"lite" => false})

      refute :meck.called(Wave3aHandlers.ProbeClient, :describe_tool, :_)
    end
  end

  describe "DescribeTool.call/1 success path" do
    test "passes the probe data payload through verbatim" do
      stub_probe_ok()

      assert {:ok, body, 200} = DescribeTool.call(%{"tool_name" => "list_tools"})
      assert body == @describe_data
    end

    test "does not forward the probe envelope wrapper" do
      stub_probe_ok()

      assert {:ok, body, 200} = DescribeTool.call(%{"tool_name" => "list_tools"})
      refute Map.has_key?(body, "ok")
      refute Map.has_key?(body, "protocol_version")
      refute Map.has_key?(body, "data")
    end

    test "semantic-error payload (success: false) rides through data unchanged" do
      # An unknown tool returns the Python error_response shape under `data`;
      # the handler surfaces it as a 200 (the probe call itself succeeded).
      error_payload = %{
        "success" => false,
        "error" => "Unknown tool: nope",
        "recovery" => %{"action" => "Call list_tools to see available tool names"}
      }

      stub_probe_ok(%{"ok" => true, "protocol_version" => "wave3a.v1", "data" => error_payload})

      assert {:ok, body, 200} = DescribeTool.call(%{"tool_name" => "nope"})
      assert body == error_payload
    end
  end

  describe "DescribeTool.call/1 error paths (shared ProbeErrors mapping)" do
    test "ProbeClient.timeout → 504 with stable error tag" do
      stub_probe_error(:timeout)

      assert {:ok, body, 504} = DescribeTool.call(%{"tool_name" => "list_tools"})
      assert body[:error] == "probe_timeout"
    end

    test "ProbeClient.connect_error → 502" do
      stub_probe_error(:connect_error)

      assert {:ok, body, 502} = DescribeTool.call(%{"tool_name" => "list_tools"})
      assert body[:error] == "probe_unavailable"
    end

    test "ProbeClient.{:non_200, status} surfaces the upstream status code" do
      stub_probe_error({:non_200, 503})

      assert {:ok, body, 502} = DescribeTool.call(%{"tool_name" => "list_tools"})
      assert body[:error] == "probe_non_200"
      assert body[:probe_status] == 503
    end
  end

  describe "HTTP router dispatch" do
    test "POST /v1/handlers/describe_tool returns the §2.2 success envelope" do
      stub_probe_ok()

      resp =
        :post
        |> conn(
          "/v1/handlers/describe_tool",
          Jason.encode!(%{"arguments" => %{"tool_name" => "list_tools"}})
        )
        |> put_req_header("content-type", "application/json")
        |> authed()
        |> HTTPRouter.call(@opts)

      assert resp.status == 200
      body = parsed(resp)

      assert body["ok"] == true
      assert body["protocol_version"] == "wave3a.v1"
      refute Map.has_key?(body, "data")
      assert body["tool"] == "list_tools"
    end

    test "missing tool_name surfaces as ok: false / 400 through the router" do
      stub_probe_ok()

      resp =
        :post
        |> conn("/v1/handlers/describe_tool", Jason.encode!(%{"arguments" => %{}}))
        |> put_req_header("content-type", "application/json")
        |> authed()
        |> HTTPRouter.call(@opts)

      assert resp.status == 400
      body = parsed(resp)
      assert body["ok"] == false
      assert body["protocol_version"] == "wave3a.v1"
      assert body["error"] == "invalid_arguments"
    end

    test "an extra argument surfaces as ok: false / 422 (Python-fallback trigger)" do
      stub_probe_ok()

      resp =
        :post
        |> conn(
          "/v1/handlers/describe_tool",
          Jason.encode!(%{"arguments" => %{"tool_name" => "list_tools", "lite" => false}})
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
