defmodule UnitaresLeasePlane.FileWriteExecutorTest do
  @moduledoc """
  Slice 2 (dry-run-first, per the dialectic resolution of 2026-06-28): the
  FileWriteExecutor validates the full lease+pre-image path but writes NO byte
  and touches NO durable state. These tests assert exactly that — the target
  file is never modified, and the result is honestly marked dry_run.
  """
  use ExUnit.Case, async: false

  alias UnitaresLeasePlane.{Canonicalize, FileWriteExecutor}

  defp sha(bytes), do: :crypto.hash(:sha256, bytes) |> Base.encode16(case: :lower)

  defp canonical_surface(path) do
    {:ok, surface} = Canonicalize.canonicalize("file://" <> path)
    surface
  end

  # This module is the only one that overrides the executor's payload ceiling,
  # so it forces a known default going IN and puts back what it found on the way
  # OUT. Resetting only on entry is not enough: whatever the LAST test in this
  # module sets stays set for the whole rest of the run, and ExUnit shuffles
  # tests within a module, so on the seeds that happen to run the ceiling test
  # last its 8-byte override escapes into every sibling module.
  #
  # That is #2152. Three governed_effect_test file_writes — payloads of 15, 28
  # and 22 bytes — came back {:error, :payload_too_large}, and master could not
  # see it because the Elixir suites are path-gated and skip on most pushes.
  # file_write_executor_commit_test.exs already carried a hand-written delete
  # against "the ceiling override" leaking from a sibling; this closes it at the
  # source instead.
  setup do
    previous = %{
      commit: Application.get_env(:lease_plane, :execute_file_write_commit_enabled),
      max_bytes: Application.get_env(:lease_plane, :file_write_payload_max_bytes)
    }

    # default state for the commit-disabled fail-safe
    Application.delete_env(:lease_plane, :execute_file_write_commit_enabled)
    Application.delete_env(:lease_plane, :file_write_payload_max_bytes)

    on_exit(fn ->
      restore_env(:execute_file_write_commit_enabled, previous.commit)
      restore_env(:file_write_payload_max_bytes, previous.max_bytes)
    end)

    :ok
  end

  defp restore_env(key, nil), do: Application.delete_env(:lease_plane, key)
  defp restore_env(key, value), do: Application.put_env(:lease_plane, key, value)

  @tag :tmp_dir
  test "dry-run validates + reads pre-image but writes NOTHING", %{tmp_dir: dir} do
    path = Path.join(dir, "note.txt")
    existing = "the existing content\n"
    File.write!(path, existing)

    leases = [%{"surface" => canonical_surface(path)}]
    payload = %{"path" => path, "content" => "the NEW content we would write\n"}

    assert {:committed, r} = FileWriteExecutor.apply_effect("e1", payload, leases)
    assert r.dry_run == true
    assert r.would_write_bytes == byte_size("the NEW content we would write\n")
    assert r.payload_sha256 == sha("the NEW content we would write\n")
    assert r.pre_image_existed == true
    assert r.pre_image_sha256 == sha(existing)
    # the load-bearing assertion: the file is UNCHANGED
    assert File.read!(path) == existing
  end

  @tag :tmp_dir
  test "dry-run on an absent target -> pre_image_existed false, file still absent", %{tmp_dir: dir} do
    path = Path.join(dir, "does-not-exist.txt")
    leases = [%{"surface" => canonical_surface(path)}]
    payload = %{"path" => path, "content" => "x"}

    assert {:committed, r} = FileWriteExecutor.apply_effect("e2", payload, leases)
    assert r.dry_run == true
    assert r.pre_image_existed == false
    assert r.pre_image_sha256 == nil
    refute File.exists?(path)
  end

  @tag :tmp_dir
  test "surface not among held leases -> rejected, never touched", %{tmp_dir: dir} do
    path = Path.join(dir, "note.txt")
    File.write!(path, "x")
    # a lease for a DIFFERENT surface
    leases = [%{"surface" => "file:///some/other/path"}]
    payload = %{"path" => path, "content" => "y"}

    assert {:rejected, :surface_path_mismatch} =
             FileWriteExecutor.apply_effect("e3", payload, leases)
    assert File.read!(path) == "x"
  end

  @tag :tmp_dir
  test "base64 content is decoded for the size/hash", %{tmp_dir: dir} do
    path = Path.join(dir, "note.txt")
    File.write!(path, "")
    raw = "hello bytes"
    payload = %{"path" => path, "content" => Base.encode64(raw), "encoding" => "base64"}
    leases = [%{"surface" => canonical_surface(path)}]

    assert {:committed, r} = FileWriteExecutor.apply_effect("e4", payload, leases)
    assert r.would_write_bytes == byte_size(raw)
    assert r.payload_sha256 == sha(raw)
  end

  @tag :tmp_dir
  test "payload over the ceiling is rejected before any work", %{tmp_dir: dir} do
    Application.put_env(:lease_plane, :file_write_payload_max_bytes, 8)
    path = Path.join(dir, "note.txt")
    File.write!(path, "x")
    leases = [%{"surface" => canonical_surface(path)}]
    payload = %{"path" => path, "content" => "this is definitely longer than eight bytes"}

    assert {:rejected, :payload_too_large} =
             FileWriteExecutor.apply_effect("e5", payload, leases)
    assert File.read!(path) == "x"
  end

  @tag :tmp_dir
  test "missing content is rejected (path resolves, content absent)", %{tmp_dir: dir} do
    path = Path.join(dir, "note.txt")
    File.write!(path, "x")
    leases = [%{"surface" => canonical_surface(path)}]
    assert {:rejected, :content_required} =
             FileWriteExecutor.apply_effect("e6", %{"path" => path}, leases)
    assert File.read!(path) == "x"
  end

  # NOTE: the prior "commit-enabled still refuses to write" fail-safe test was
  # removed when the live commit path landed — commit-enabled now performs the
  # real write. The default (commit DISABLED -> dry-run) fail-safe is still
  # exercised by every test above (none of which sets the commit flag); the live
  # commit + compensation is covered by file_write_executor_commit_test.exs.

  test "executor declares itself reversible" do
    assert FileWriteExecutor.reversible?() == true
  end
end
