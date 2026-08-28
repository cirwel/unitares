defmodule AgentOrchestrator.PostgresIdempotencyLedgerTest do
  use ExUnit.Case, async: true

  alias AgentOrchestrator.PostgresIdempotencyLedger

  @digest String.duplicate("a", 64)
  @other_digest String.duplicate("b", 64)
  @candidate "ex-11111111-1111-4111-8111-111111111111"
  @stored "ex-22222222-2222-4222-8222-222222222222"

  test "classifies an atomic reservation claimed by this caller" do
    assert {:ok, :reserved} =
             PostgresIdempotencyLedger.classify_reservation(
               @digest,
               @candidate,
               "reserved",
               @digest,
               @candidate
             )
  end

  test "replays started rows and preserves reserved ambiguity" do
    assert {:ok, {:replay, @stored, :started}} =
             PostgresIdempotencyLedger.classify_reservation(
               @digest,
               @stored,
               "started",
               @digest,
               @candidate
             )

    assert {:ok, {:replay, @stored, :reserved}} =
             PostgresIdempotencyLedger.classify_reservation(
               @digest,
               @stored,
               "reserved",
               @digest,
               @candidate
             )
  end

  test "same key with another material spec fails closed" do
    assert {:error, :idempotency_conflict} =
             PostgresIdempotencyLedger.classify_reservation(
               @other_digest,
               @stored,
               "started",
               @digest,
               @candidate
             )
  end

  test "reservation SQL atomically reclaims only expired keys" do
    sql = PostgresIdempotencyLedger.reserve_sql()
    assert sql =~ "orchestration.spawn_idempotency"
    assert sql =~ "ON CONFLICT (key_hash) DO UPDATE"
    assert sql =~ "existing.expires_at <= now()"
    refute sql =~ "cmd"
    refute sql =~ "env"
    refute sql =~ "output"
  end
end
