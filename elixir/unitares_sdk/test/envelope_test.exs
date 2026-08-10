defmodule UnitaresSdk.EnvelopeTest do
  use ExUnit.Case, async: true

  alias UnitaresSdk.Envelope

  describe "the four observed envelope shapes" do
    test "dispatch_beam: result is a map" do
      assert {:ok, %{"a" => 1}} = Envelope.decode(~s({"result": {"a": 1}}))
    end

    test "dispatch_beam: result is a JSON string needing a second decode" do
      # A single decode leaves the caller holding a blob it will silently fail
      # to read fields out of.
      assert {:ok, %{"a" => 1}} = Envelope.decode(~s({"result": "{\\"a\\": 1}"}))
    end

    test "sentinel: success-wrapped result" do
      assert {:ok, %{"a" => 1}} = Envelope.decode(~s({"success": true, "result": {"a": 1}}))
    end

    test "dialectic_live: bare payload with no result key" do
      assert {:ok, %{"sessions" => []}} = Envelope.decode(~s({"sessions": []}))
    end

    test "a non-object string result is legitimate output, not a failure" do
      assert {:ok, %{"result" => "plain text"}} = Envelope.decode(~s({"result": "plain text"}))
    end
  end

  describe "regression: the silent default-proceed (Lumen, ~3 days dark)" do
    # A typed strict refusal carries NEITHER success:false NOR an action. Three
    # of the four in-fleet decoders read it as a clean result with no verdict,
    # i.e. "proceed". This is the pin that stops that shape from ever again
    # leaving {:ok, _}.
    @refusal ~s({"status": "identity_required", "error_code": "SESSION_ERROR", "error_category": "auth_error"})

    test "is an error, never {:ok, _}" do
      assert {:error, {:refused, :identity_required}} = Envelope.decode(@refusal)
    end

    test "carries no success:false to key off — the naive guard would miss it" do
      {:ok, decoded} = {:ok, :json.decode(@refusal)}
      refute Map.has_key?(decoded, "success")
      refute Map.has_key?(decoded, "action")
    end

    test "is caught when nested inside a clean-looking result wrapper" do
      body = ~s({"success": true, "result": #{@refusal}})
      assert {:error, {:refused, :identity_required}} = Envelope.decode(body)
    end

    test "error_code alone is enough" do
      assert {:error, {:refused, :session_error}} =
               Envelope.decode(~s({"error_code": "SESSION_ERROR"}))
    end

    test "error_category alone is enough" do
      assert {:error, {:refused, :auth_error}} =
               Envelope.decode(~s({"error_category": "auth_error"}))
    end
  end

  describe "regression: the swallowed pause (Sentinel, ~18h dark)" do
    test "AGENT_PAUSED is tagged distinctly, not as a generic tool error" do
      assert {:error, {:refused, :agent_paused}} =
               Envelope.decode(~s({"error_code": "AGENT_PAUSED"}))
    end

    test "AGENT_PAUSED as a status is caught too" do
      assert {:error, {:refused, :agent_paused}} =
               Envelope.decode(~s({"status": "AGENT_PAUSED"}))
    end

    # Surfaced by the third consumer (elixir/sentinel), which posts
    # process_agent_update and reads the verdict from "action". An earlier
    # revision of this module treated action:"pause" as a refusal, which would
    # have converted every legitimate pause verdict into an error and hidden it
    # from the code whose entire job is to act on it.
    test "a pause VERDICT is not a refusal — action is the answer, not an error" do
      assert {:ok, %{"action" => "pause"}} =
               Envelope.decode(~s({"result": {"action": "pause", "confidence": 0.4}}))
    end

    test "a pause verdict survives the success wrapper too" do
      assert {:ok, %{"action" => "pause"}} =
               Envelope.decode(~s({"success": true, "result": {"action": "pause"}}))
    end
  end

  describe "ordinary failures" do
    test "explicit success:false is a tool error" do
      assert {:error, {:tool_error, _}} = Envelope.decode(~s({"success": false, "detail": "x"}))
    end

    test "malformed json" do
      assert {:error, :bad_json} = Envelope.decode("not json at all")
    end

    test "a non-object top level is not a payload" do
      assert {:error, {:unexpected_payload, _}} = Envelope.decode("[1, 2, 3]")
    end
  end
end
