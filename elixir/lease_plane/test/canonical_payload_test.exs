defmodule UnitaresLeasePlane.CanonicalPayloadTest do
  @moduledoc """
  Pins the Elixir half of the cross-language canonical payload form against
  the shared fixture `tests/vectors/effect_payload_canonical.json` (repo
  root). The Python half (`unitares_sdk.lease_plane.canonical`) consumes the
  same file — a green run on both sides is the byte-identity proof #1252
  item 1 requires.
  """
  use ExUnit.Case, async: true

  alias UnitaresLeasePlane.CanonicalPayload

  @vectors_path Path.expand(
                  "../../../tests/vectors/effect_payload_canonical.json",
                  __DIR__
                )

  defp fixture do
    @vectors_path |> File.read!() |> Jason.decode!()
  end

  test "reproduces every shared vector byte-identically" do
    for %{"name" => name, "payload" => payload, "canonical" => canonical, "sha256" => sha} <-
          fixture()["vectors"] do
      assert {:ok, bytes} = CanonicalPayload.bytes(payload), "vector #{name}: bytes/1 refused"
      assert bytes == canonical, "vector #{name}: canonical bytes diverge"
      assert {:ok, ^sha} = CanonicalPayload.sha256(payload), "vector #{name}: sha256 diverges"
    end
  end

  test "rejects every shared reject vector" do
    for %{"name" => name, "payload" => payload} <- fixture()["rejects"] do
      assert {:error, _} = CanonicalPayload.sha256(payload), "reject #{name}: was accepted"
    end
  end

  test "rejects a non-map payload and unsupported types" do
    assert {:error, :not_a_map} = CanonicalPayload.bytes("nope")
    assert {:error, :unsupported_type} = CanonicalPayload.bytes(%{"pid" => self()})
    assert {:error, :non_string_key} = CanonicalPayload.bytes(%{1 => "v"})
  end

  test "float and control-char rejection is deep" do
    assert {:error, :float_in_payload} = CanonicalPayload.sha256(%{"a" => [%{"b" => [1.0]}]})

    assert {:error, :control_char_in_payload} =
             CanonicalPayload.sha256(%{"a" => [%{"b" => <<"x", 0x1B, "y">>}]})
  end
end
