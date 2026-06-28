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

  setup do
    # default state for the commit-disabled fail-safe
    Application.delete_env(:lease_plane, :execute_file_write_commit_enabled)
    Application.delete_env(:lease_plane, :file_write_payload_max_bytes)
    :ok
  end

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
