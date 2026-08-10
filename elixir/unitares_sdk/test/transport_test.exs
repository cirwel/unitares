defmodule UnitaresSdk.TransportTest do
  use ExUnit.Case, async: true

  alias UnitaresSdk.Transport

  describe "url handling" do
    test "trailing slash does not double up the tool path" do
      me = self()

      poster = fn url, _body, _opts ->
        send(me, {:url, url})
        {:ok, 200, ~s({"result": {}})}
      end

      Transport.call_tool("http://x:8767/", "health_check", %{}, http_post: poster)
      assert_receive {:url, "http://x:8767/v1/tools/call"}
    end

    test "nil url means governance disabled, not an error to log" do
      assert {:error, :disabled} = Transport.call_tool(nil, "onboard", %{})
    end

    test "normalize_url treats empty as disabled" do
      assert Transport.normalize_url("") == nil
      assert Transport.normalize_url(nil) == nil
    end
  end

  describe "request shape" do
    test "posts {name, arguments}" do
      me = self()

      poster = fn _url, body, _opts ->
        send(me, {:body, :json.decode(body)})
        {:ok, 200, ~s({"result": {}})}
      end

      Transport.call_tool("http://x", "onboard", %{"force_new" => true}, http_post: poster)
      assert_receive {:body, %{"name" => "onboard", "arguments" => %{"force_new" => true}}}
    end
  end

  describe "failure posture" do
    test "non-200 is tagged with the code" do
      poster = fn _u, _b, _o -> {:ok, 503, "nope"} end
      assert {:error, {:http, 503}} = Transport.call_tool("http://x", "t", %{}, http_post: poster)
    end

    test "transport error passes through" do
      poster = fn _u, _b, _o -> {:error, :timeout} end
      assert {:error, :timeout} = Transport.call_tool("http://x", "t", %{}, http_post: poster)
    end

    test "a refusal survives the transport layer as an error" do
      body = ~s({"status": "identity_required", "error_code": "SESSION_ERROR"})
      poster = fn _u, _b, _o -> {:ok, 200, body} end

      assert {:error, {:refused, :identity_required}} =
               Transport.call_tool("http://x", "sync_state", %{}, http_post: poster)
    end
  end

  describe "timeout budgets" do
    test "onboard gets a larger budget than an ordinary call" do
      # A single 4s onboard timeout once left a governance feed dark 9.5h.
      assert Transport.onboard_timeout() > Transport.default_timeout()
    end

    test "the default timeout is passed through" do
      me = self()

      poster = fn _u, _b, opts ->
        send(me, {:timeout, Keyword.fetch!(opts, :timeout)})
        {:ok, 200, ~s({"result": {}})}
      end

      Transport.call_tool("http://x", "t", %{}, http_post: poster)
      assert_receive {:timeout, 4_000}
    end
  end
end
