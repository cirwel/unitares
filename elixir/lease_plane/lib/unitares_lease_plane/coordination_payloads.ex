defmodule UnitaresLeasePlane.CoordinationPayloads do
  @moduledoc """
  Elixir side of the Wave 3 §6.4 payload-construction contract — the BEAM
  mirror of `governance_core/coordination_events_helpers.py`. Every BEAM
  emission of a `coordination_failure.beam_python_boundary.*` event or a
  `measurement.*` row builds its payload here; inline map construction at
  emission sites is prohibited and lint-checked by
  `scripts/dev/check-boundary-event-helpers.sh` (the dotted event-type
  literals may appear only in the canonical constants/helpers modules).

  Ships ahead of its BEAM emitters per the Wave 3a constants precedent
  (§14 prereq PR #3): the contract lands first so the Wave 3 wiring PRs
  (#2, #8a/#8b, #10) emit without re-deriving payload shapes. The lease
  plane is the natural host — it is the first BEAM process with a Postgrex
  pool on the governance DB; the orchestrator and wave3a_handlers alias
  this module rather than fork it.

  Payload contracts are pinned in `src/coordination_events.py` (§8.4 block)
  and mirrored by the Python helpers; the two sides are kept aligned by the
  shared lint plus the parity notes here. Keys are strings (JSON-bound).
  """

  @valid_error_classes ~w(timeout connect_error non_200 decode_error other)

  # --- Event-type strings (BEAM side of the §8.4 / §6.3 constants) --------
  # The Python constants in src/coordination_events.py are canonical; these
  # functions are the only legal source of the literals in elixir/.

  def boundary_python_to_beam_failed_type,
    do: "coordination_failure.beam_python_boundary.python_to_beam_request_failed"

  def boundary_beam_to_python_failed_type,
    do: "coordination_failure.beam_python_boundary.beam_to_python_request_failed"

  def ets_pg_divergence_type,
    do: "coordination_failure.beam_python_boundary.ets_pg_divergence"

  def measurement_boundary_request_type,
    do: "measurement.beam_python_boundary.request"

  def measurement_lease_plane_request_type,
    do: "measurement.lease_plane.request"

  # --- Payload constructors ------------------------------------------------

  @doc """
  Failure-channel payload (mirrors Python `make_boundary_payload`).

  Raises `ArgumentError` on: empty endpoint/method, error_class outside the
  documented enum, `status_code` nil when `error_class == "non_200"`, or
  wrong types for status_code/elapsed_ms.
  """
  def boundary_payload(endpoint, method, error_class, status_code, elapsed_ms) do
    validate_nonempty!(endpoint, "endpoint")
    validate_nonempty!(method, "method")

    unless error_class in @valid_error_classes do
      raise ArgumentError,
            "error_class=#{inspect(error_class)} not in #{inspect(@valid_error_classes)}"
    end

    if error_class == "non_200" and is_nil(status_code) do
      raise ArgumentError, "status_code is required when error_class == non_200"
    end

    validate_int_or_nil!(status_code, "status_code")
    validate_int_or_nil!(elapsed_ms, "elapsed_ms")

    %{
      "endpoint" => endpoint,
      "method" => method,
      "error_class" => error_class,
      "status_code" => status_code,
      "elapsed_ms" => elapsed_ms
    }
  end

  @doc """
  Measurement-channel payload (mirrors Python `make_measurement_payload`).

  `status_code` may be nil (transport-level failures measured before a
  status line exists). `elapsed_ms` is required and non-negative;
  `payload_bytes` is nil or a non-negative integer.
  """
  def measurement_payload(endpoint, method, status_code, elapsed_ms, payload_bytes) do
    validate_nonempty!(endpoint, "endpoint")
    validate_nonempty!(method, "method")
    validate_int_or_nil!(status_code, "status_code")

    unless is_integer(elapsed_ms) and elapsed_ms >= 0 do
      raise ArgumentError, "elapsed_ms must be a non-negative integer"
    end

    unless is_nil(payload_bytes) or (is_integer(payload_bytes) and payload_bytes >= 0) do
      raise ArgumentError, "payload_bytes must be nil or a non-negative integer"
    end

    %{
      "endpoint" => endpoint,
      "method" => method,
      "status_code" => status_code,
      "elapsed_ms" => elapsed_ms,
      "payload_bytes" => payload_bytes
    }
  end

  defp validate_nonempty!(value, name) do
    unless is_binary(value) and String.trim(value) != "" do
      raise ArgumentError, "#{name} must be a non-empty string"
    end
  end

  defp validate_int_or_nil!(value, name) do
    unless is_nil(value) or is_integer(value) do
      raise ArgumentError, "#{name} must be nil or an integer"
    end
  end
end
