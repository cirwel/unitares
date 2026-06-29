defmodule UnitaresLeasePlane.EffectRepoContractTest do
  @moduledoc """
  Real-DB contract tests for the effects.payloads write path.

  These exist because the file_write commit bug (#1204) — a committed file with
  NO durable row — PASSED its unit tests: the FakeRepo's record_pre_image
  returned :ok regardless, hiding that the REAL record_pre_image is an UPDATE
  that needs the row to already exist. A real-DB test cannot lie about that.
  """
  use ExUnit.Case, async: false

  alias UnitaresLeasePlane.EffectRepo

  setup do
    # Skip cleanly (don't error) if migration 052 hasn't reached this test DB.
    case Postgrex.query(UnitaresLeasePlane.DB, "SELECT to_regclass('effects.payloads')", []) do
      {:ok, %{rows: [[nil]]}} ->
        {:skip, "effects.payloads absent — migration 052 not applied to the test DB"}

      _ ->
        :ok
    end
  end

  defp eid(suffix), do: "ge-contract-#{suffix}-#{System.unique_integer([:positive])}"

  defp cleanup(id) do
    on_exit(fn ->
      Postgrex.query(UnitaresLeasePlane.DB, "DELETE FROM effects.payloads WHERE effect_id = $1", [id])
    end)
  end

  defp insert(id) do
    EffectRepo.insert_effect_payload(%{
      effect_id: id,
      effect_type: "file_write",
      payload_bytes: "content",
      payload_sha256: "psha",
      required_leases: [],
      proposer_agent_uuid: nil,
      idempotency_key: "k-#{id}",
      idempotency_digest: "d-#{id}"
    })
  end

  test "record_pre_image is UPDATE-only: a never-inserted effect is :already and creates NO row" do
    id = eid("noinsert")
    cleanup(id)

    # THE BUG-CATCHER. The dispatch MUST insert the row first; skipping the insert
    # means record_pre_image touches nothing, and a subsequent commit would be
    # untracked — exactly #1204, which the FakeRepo's blanket :ok masked.
    assert EffectRepo.record_pre_image(id, "sha", "bytes", true) == :already
    assert {:ok, nil} = EffectRepo.get_payload(id)
  end

  test "full contract: insert -> record_pre_image -> mark_committed = a tracked, committed row" do
    id = eid("full")
    cleanup(id)

    assert :ok = insert(id)
    assert {:ok, row} = EffectRepo.get_payload(id)
    refute row[:committed_at]
    refute row[:rollback_state]

    assert :ok = EffectRepo.record_pre_image(id, "presha", "prebytes", true)
    assert {:ok, row2} = EffectRepo.get_payload(id)
    assert row2[:rollback_state] == "pending"
    assert row2[:pre_image_sha256] == "presha"

    assert :ok = EffectRepo.mark_committed(id)
    assert {:ok, row3} = EffectRepo.get_payload(id)
    assert row3[:committed_at]
    # mark_committed deliberately nulls rollback_state (committed = resolved)
    refute row3[:rollback_state]
  end

  test "record_pre_image is idempotent: a second call after the pre-image is :already" do
    id = eid("idem")
    cleanup(id)

    assert :ok = insert(id)
    assert :ok = EffectRepo.record_pre_image(id, "s", "b", false)
    # rollback_state is no longer NULL -> the WHERE matches 0 rows -> :already
    assert :already = EffectRepo.record_pre_image(id, "s2", "b2", false)
  end
end
