defmodule UnitaresLeasePlane.EffectReconcileTest do
  @moduledoc """
  The crash-recovery reconciliation core (§5b) — the safety-critical dispatch.
  Asserts the corruption defense holds: recovery NEVER writes the file, only a
  DB mark (commit-forward / tombstone / quarantine), and a DIRTY surface (a
  competing writer's bytes) is quarantined, never clobbered.

  Uses a fake repo that records which mark was called (no DB) + real temp files.
  """
  use ExUnit.Case, async: true

  alias UnitaresLeasePlane.{EffectReconcile, EffectRecovery}

  defmodule FakeRepo do
    def mark_committed(id), do: record({:mark_committed, id})
    def tombstone(id), do: record({:tombstone, id})
    def quarantine(id), do: record({:quarantine, id})
    # used by EffectRecovery.scan tests; set the canned return via the proc dict
    def orphaned_payloads, do: Process.get(:fake_orphans, {:ok, []})
    defp record(msg), do: (send(self(), msg) && :ok)
  end

  defp sha(bytes), do: EffectReconcile.sha256_hex(bytes)

  defp payload(path, opts) do
    %{
      effect_id: "eff-1",
      payload_sha256: Keyword.fetch!(opts, :payload_sha256),
      pre_image_sha256: Keyword.get(opts, :pre_image_sha256),
      pre_image_existed: Keyword.get(opts, :pre_image_existed, true),
      required_leases: [%{"surface" => "file://" <> path}]
    }
  end

  @tag :tmp_dir
  test "write completed pre-crash (file == payload bytes) -> commit-forward, file untouched", %{tmp_dir: dir} do
    path = Path.join(dir, "note.txt")
    new_bytes = "the committed content\n"
    File.write!(path, new_bytes)

    p = payload(path, payload_sha256: sha(new_bytes), pre_image_sha256: sha("old\n"))
    assert EffectReconcile.reconcile_payload(p, FakeRepo) == :committed
    assert_received {:mark_committed, "eff-1"}
    # corruption defense: the file is NOT rewritten
    assert File.read!(path) == new_bytes
  end

  @tag :tmp_dir
  test "surface at pre-image (file == pre_image bytes) -> tombstone (retry re-executes)", %{tmp_dir: dir} do
    path = Path.join(dir, "note.txt")
    pre = "the original content\n"
    File.write!(path, pre)

    p = payload(path, payload_sha256: sha("would-have-written\n"), pre_image_sha256: sha(pre))
    assert EffectReconcile.reconcile_payload(p, FakeRepo) == :tombstoned
    assert_received {:tombstone, "eff-1"}
    assert File.read!(path) == pre
  end

  @tag :tmp_dir
  test "pre-image-absent and file absent -> tombstone", %{tmp_dir: dir} do
    path = Path.join(dir, "never-created.txt")
    refute File.exists?(path)

    p = payload(path, payload_sha256: sha("x"), pre_image_sha256: nil, pre_image_existed: false)
    assert EffectReconcile.reconcile_payload(p, FakeRepo) == :tombstoned
    assert_received {:tombstone, "eff-1"}
    refute File.exists?(path)
  end

  @tag :tmp_dir
  test "DIRTY surface (competing writer's bytes, neither hash) -> quarantine, NEVER clobbered", %{tmp_dir: dir} do
    path = Path.join(dir, "note.txt")
    competing = "a DIFFERENT writer got here\n"
    File.write!(path, competing)

    p = payload(path, payload_sha256: sha("our-payload\n"), pre_image_sha256: sha("our-pre-image\n"))
    assert EffectReconcile.reconcile_payload(p, FakeRepo) == {:quarantined, :dirty}
    assert_received {:quarantine, "eff-1"}
    # the competing writer's content is left exactly as-is
    assert File.read!(path) == competing
  end

  test "unresolvable surface -> quarantine (cannot prove safety)" do
    p = %{effect_id: "eff-x", payload_sha256: "a", pre_image_sha256: nil,
          pre_image_existed: false, required_leases: []}
    assert {:quarantined, {:surface, _}} = EffectReconcile.reconcile_payload(p, FakeRepo)
    assert_received {:quarantine, "eff-x"}
  end

  test "required_leases as a raw JSON string is decoded" do
    p = %{effect_id: "eff-j", payload_sha256: "a", pre_image_sha256: nil,
          pre_image_existed: false, required_leases: ~s([{"surface":"file:///no/such/file"}])}
    # file absent + pre_image_existed false -> tombstone
    assert EffectReconcile.reconcile_payload(p, FakeRepo) == :tombstoned
    assert_received {:tombstone, "eff-j"}
  end

  # --- EffectRecovery boot scanner (fail-soft) -------------------------------

  test "recovery scan is FAIL-SOFT when the effects table is missing" do
    Process.put(:fake_orphans, {:error, %{postgres: %{code: :undefined_table}}})
    result = EffectRecovery.scan(FakeRepo)
    assert %{scanned: 0, skipped: _} = result
  end

  test "recovery scan drains orphans and tallies outcomes" do
    # one committed-forward, one tombstone (both via a temp file)
    dir = System.tmp_dir!()
    committed_path = Path.join(dir, "rec-committed-#{System.unique_integer([:positive])}.txt")
    File.write!(committed_path, "done\n")

    orphans = [
      %{effect_id: "o1", payload_sha256: sha("done\n"), pre_image_sha256: nil,
        pre_image_existed: false, required_leases: [%{"surface" => "file://" <> committed_path}]}
    ]

    Process.put(:fake_orphans, {:ok, orphans})
    result = EffectRecovery.scan(FakeRepo)
    assert result.scanned == 1
    assert result.outcomes[:committed] == 1
    assert_received {:mark_committed, "o1"}
  after
    Process.delete(:fake_orphans)
  end
end
