defmodule DialecticLive.GovernanceTest do
  use ExUnit.Case, async: true

  alias UnitaresSdk.Envelope

  # This module's HTTP transport stays Req; what moved to the SDK is reading
  # the reply. These tests pin the contract `call/2` now depends on, at the
  # level this app can reach without standing up a governance server.

  describe "the bug this migration fixed" do
    # Before: `unwrap/1` returned any non-"result" map as a successful result,
    # and `normalize_sessions/1` — which tolerantly accepts sessions/items/data
    # and falls back to [] — turned it into an empty list. So a governance
    # refusal rendered in the pane as "no dialectic sessions".
    #
    # The tolerance is correct and stays; it just must never see a refusal.
    @refusal %{
      "status" => "identity_required",
      "error_code" => "SESSION_ERROR",
      "error_category" => "auth_error"
    }

    test "a refusal classifies as an error, so it can never reach normalize_sessions" do
      assert {:error, {:refused, :identity_required}} = Envelope.classify(@refusal)
    end

    test "the refusal has none of the keys normalize_sessions looks for" do
      # This is why it used to render as an empty list rather than an error:
      # every recognised shape is absent, so the tolerant fallback caught it.
      refute Map.has_key?(@refusal, "sessions")
      refute Map.has_key?(@refusal, "items")
      refute Map.has_key?(@refusal, "data")
      refute is_list(@refusal)
    end
  end

  describe "shapes this pane must keep accepting" do
    test "sessions under the result wrapper" do
      assert {:ok, %{"sessions" => [%{"id" => "s1"}]}} =
               Envelope.classify(%{"result" => %{"sessions" => [%{"id" => "s1"}]}})
    end

    test "a bare list result" do
      assert {:ok, %{"result" => [%{"id" => "s1"}]}} =
               Envelope.classify(%{"result" => [%{"id" => "s1"}]})
    end

    test "a flat payload with no result wrapper" do
      assert {:ok, %{"items" => []}} = Envelope.classify(%{"items" => []})
    end
  end

  test "config/0 still reads this app's governance keyword list" do
    assert is_list(DialecticLive.Governance.config())
  end
end
