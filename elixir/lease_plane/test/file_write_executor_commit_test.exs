defmodule UnitaresLeasePlane.FileWriteExecutorCommitTest do
  @moduledoc """
  The live commit + in-process compensation path, with FAULT INJECTION AT EVERY
  STEP — the hard precondition the slice-2 dialectic review set before the commit
  flag may ever flip. slice 1b tested CRASH recovery (which never writes); this
  tests the live-write-then-restore path it left untested.

  File ops and the repo are injected (Application env), so each step can be made
  to fail deterministically: the commit write, the restore write, the rm, the
  pre-image persist, and the committed-mark.
  """
  use ExUnit.Case, async: false

  alias UnitaresLeasePlane.{Canonicalize, FileWriteExecutor}

  # --- injected fakes --------------------------------------------------------

  defmodule FakeFileOps do
    def read(_path), do: rec({:read}, Process.get(:read_result, {:error, :enoent}))

    def write(path, bytes), do: rec({:write, path, bytes}, next_write())

    def rm(_path), do: rec({:rm}, Process.get(:rm_result, :ok))

    defp next_write do
      case Process.get(:write_results, [:ok]) do
        [r | rest] -> Process.put(:write_results, rest) && r
        [] -> :ok
      end
    end

    defp rec(call, result), do: (send(self(), {:fileop, call}) && result)
  end

  # NOTE: this fake is deliberately lenient — it exercises the EXECUTOR's commit
  # logic in isolation (write / mark / compensation), assuming the durable row
  # already exists (the DISPATCH inserts it). It does NOT model the real
  # UPDATE-only semantics of record_pre_image; that contract — and the #1204
  # "committed file, no durable row" bug it once hid — is pinned by the real-DB
  # effect_repo_contract_test.exs.
  defmodule FakeRepo do
    def record_pre_image(_id, _sha, _bytes, _existed?),
      do: rec(:record_pre_image, Process.get(:record_result, :ok))

    def mark_committed(_id), do: rec(:mark_committed, Process.get(:mark_result, :ok))
    def tombstone(id), do: rec({:tombstone, id}, :ok)
    def quarantine(id), do: rec({:quarantine, id}, :ok)
    defp rec(tag, result), do: (send(self(), {:repo, tag}) && result)
  end

  setup %{tmp_dir: dir} do
    # reset any env a sibling test file may have leaked (e.g. the ceiling override)
    Application.delete_env(:lease_plane, :file_write_payload_max_bytes)
    Application.put_env(:lease_plane, :effect_file_ops, FakeFileOps)
    Application.put_env(:lease_plane, :effect_repo, FakeRepo)
    Application.put_env(:lease_plane, :execute_file_write_commit_enabled, true)

    on_exit(fn ->
      Application.delete_env(:lease_plane, :effect_file_ops)
      Application.delete_env(:lease_plane, :effect_repo)
      Application.delete_env(:lease_plane, :execute_file_write_commit_enabled)
    end)

    path = Path.join(dir, "note.txt")
    {:ok, surface} = Canonicalize.canonicalize("file://" <> path)
    leases = [%{"surface" => surface}]
    payload = %{"path" => path, "content" => "the new content\n"}
    %{leases: leases, payload: payload}
  end

  @tag :tmp_dir
  test "happy commit: pre-image persisted, write, mark_committed", %{payload: p, leases: l} do
    Process.put(:read_result, {:ok, "old\n"})
    Process.put(:write_results, [:ok])
    Process.put(:mark_result, :ok)

    assert {:committed, r} = FileWriteExecutor.apply_effect("eC1", p, l)
    assert r.bytes_written == byte_size("the new content\n")
    refute Map.has_key?(r, :dry_run)
    refute Map.has_key?(r, :mark_deferred)
    assert_received {:repo, :record_pre_image}
    assert_received {:fileop, {:write, _, "the new content\n"}}
    assert_received {:repo, :mark_committed}
  end

  @tag :tmp_dir
  test "STEP write fails + restore (existed) succeeds -> TOMBSTONE", %{payload: p, leases: l} do
    Process.put(:read_result, {:ok, "the original\n"})
    # commit write fails, restore write succeeds
    Process.put(:write_results, [{:error, :eio}, :ok])

    assert {:rejected, {:committed_failed_rolled_back, :eio}} =
             FileWriteExecutor.apply_effect("eC2", p, l)
    # the restore wrote the pre-image bytes back
    assert_received {:fileop, {:write, _, "the original\n"}}
    assert_received {:repo, {:tombstone, "eC2"}}
    refute_received {:repo, {:quarantine, _}}
  end

  @tag :tmp_dir
  test "STEP write fails + restore ALSO fails -> QUARANTINE", %{payload: p, leases: l} do
    Process.put(:read_result, {:ok, "orig\n"})
    Process.put(:write_results, [{:error, :eio}, {:error, :erofs}])

    assert {:rejected, :rollback_failed} = FileWriteExecutor.apply_effect("eC3", p, l)
    assert_received {:repo, {:quarantine, "eC3"}}
    refute_received {:repo, {:tombstone, _}}
  end

  @tag :tmp_dir
  test "STEP write fails + target did NOT exist + rm succeeds -> TOMBSTONE", %{payload: p, leases: l} do
    Process.put(:read_result, {:error, :enoent})
    Process.put(:write_results, [{:error, :eio}])
    Process.put(:rm_result, :ok)

    assert {:rejected, {:committed_failed_rolled_back, :eio}} =
             FileWriteExecutor.apply_effect("eC4", p, l)
    assert_received {:fileop, {:rm}}
    assert_received {:repo, {:tombstone, "eC4"}}
  end

  @tag :tmp_dir
  test "STEP write succeeds but mark_committed fails -> COMMITTED (never undo a real write)",
       %{payload: p, leases: l} do
    Process.put(:read_result, {:ok, "old\n"})
    Process.put(:write_results, [:ok])
    Process.put(:mark_result, {:error, :db_down})

    assert {:committed, r} = FileWriteExecutor.apply_effect("eC5", p, l)
    assert r.mark_deferred == true
    # the commit write happened; crucially NO compensation write follows it
    assert_received {:fileop, {:write, _, "the new content\n"}}
    refute_received {:repo, {:tombstone, _}}
    refute_received {:repo, {:quarantine, _}}
  end

  @tag :tmp_dir
  test "STEP pre-image persist fails -> REJECTED, no write attempted", %{payload: p, leases: l} do
    Process.put(:read_result, {:ok, "old\n"})
    Process.put(:record_result, {:error, :db_down})

    assert {:rejected, {:persist_failed, :db_down}} = FileWriteExecutor.apply_effect("eC6", p, l)
    refute_received {:fileop, {:write, _, _}}
  end
end
