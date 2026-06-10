defmodule UnitaresLeasePlane.CoordinationPayloadsTest do
  use ExUnit.Case, async: true

  alias UnitaresLeasePlane.CoordinationPayloads, as: CP

  describe "boundary_payload/5" do
    test "happy path returns the pinned five-key map" do
      payload = CP.boundary_payload("gov/process_agent_update", "POST", "non_200", 502, 140)

      assert payload == %{
               "endpoint" => "gov/process_agent_update",
               "method" => "POST",
               "error_class" => "non_200",
               "status_code" => 502,
               "elapsed_ms" => 140
             }
    end

    test "timeout with nil status_code is legal" do
      payload = CP.boundary_payload("x", "GET", "timeout", nil, 2050)
      assert payload["status_code"] == nil
    end

    test "non_200 without status_code raises" do
      assert_raise ArgumentError, ~r/status_code is required/, fn ->
        CP.boundary_payload("x", "POST", "non_200", nil, 1)
      end
    end

    test "unknown error_class raises" do
      assert_raise ArgumentError, ~r/error_class/, fn ->
        CP.boundary_payload("x", "POST", "weird", nil, 1)
      end
    end

    test "empty endpoint raises" do
      assert_raise ArgumentError, ~r/endpoint/, fn ->
        CP.boundary_payload("  ", "POST", "other", nil, 1)
      end
    end
  end

  describe "measurement_payload/5" do
    test "happy path returns the pinned five-key map" do
      payload = CP.measurement_payload("lease_plane/v1/lease/acquire", "POST", 200, 28, 512)

      assert payload == %{
               "endpoint" => "lease_plane/v1/lease/acquire",
               "method" => "POST",
               "status_code" => 200,
               "elapsed_ms" => 28,
               "payload_bytes" => 512
             }
    end

    test "nil status_code and nil payload_bytes are legal" do
      payload = CP.measurement_payload("x", "POST", nil, 0, nil)
      assert payload["status_code"] == nil
      assert payload["payload_bytes"] == nil
    end

    test "negative elapsed_ms raises" do
      assert_raise ArgumentError, ~r/elapsed_ms/, fn ->
        CP.measurement_payload("x", "POST", 200, -1, nil)
      end
    end

    test "negative payload_bytes raises" do
      assert_raise ArgumentError, ~r/payload_bytes/, fn ->
        CP.measurement_payload("x", "POST", 200, 1, -5)
      end
    end

    test "empty method raises" do
      assert_raise ArgumentError, ~r/method/, fn ->
        CP.measurement_payload("x", "", 200, 1, nil)
      end
    end
  end

  describe "event-type strings" do
    test "match the canonical Python constants byte-for-byte" do
      assert CP.boundary_python_to_beam_failed_type() ==
               "coordination_failure.beam_python_boundary.python_to_beam_request_failed"

      assert CP.boundary_beam_to_python_failed_type() ==
               "coordination_failure.beam_python_boundary.beam_to_python_request_failed"

      assert CP.ets_pg_divergence_type() ==
               "coordination_failure.beam_python_boundary.ets_pg_divergence"

      assert CP.measurement_boundary_request_type() == "measurement.beam_python_boundary.request"
      assert CP.measurement_lease_plane_request_type() == "measurement.lease_plane.request"
    end
  end
end
