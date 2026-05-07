defmodule UnitaresSentinel.AtomicWriteTest do
  use ExUnit.Case, async: true

  alias UnitaresSentinel.AtomicWrite

  setup do
    tmpdir = System.tmp_dir!() |> Path.join("unitares_sentinel_atomic_write_test_#{System.unique_integer([:positive])}")
    File.mkdir_p!(tmpdir)
    on_exit(fn -> File.rm_rf!(tmpdir) end)
    {:ok, tmpdir: tmpdir}
  end

  test "writes content atomically and the file holds exactly that content", %{tmpdir: tmpdir} do
    path = Path.join(tmpdir, "anchor.json")
    content = ~s({"agent_uuid": "abc", "continuity_token": "v1.eyJ..."})

    :ok = AtomicWrite.write(path, content)

    assert File.read!(path) == content
  end

  test "the written file has mode 0o600 (binding per RFC v0.1.1 §B1 reviewer)", %{tmpdir: tmpdir} do
    path = Path.join(tmpdir, "anchor.json")
    :ok = AtomicWrite.write(path, "secret-credential-payload")

    %File.Stat{mode: mode} = File.stat!(path)
    # mode is the full 32-bit st_mode; mask off file-type bits to compare permissions only.
    permission_bits = Bitwise.band(mode, 0o777)
    assert permission_bits == 0o600,
           "atomic write must produce mode 0o600 — got #{inspect(Integer.to_string(permission_bits, 8))}"
  end

  test "tmp file is cleaned up on success (no orphan)", %{tmpdir: tmpdir} do
    path = Path.join(tmpdir, "anchor.json")
    :ok = AtomicWrite.write(path, "hello")

    refute File.exists?(path <> ".tmp"),
           "atomic write must leave no orphan .tmp file after a successful rename"
  end

  test "tmp file is cleaned up on rename failure (orphan-cleanup invariant)", %{tmpdir: tmpdir} do
    # Force a rename failure by making the destination a directory (POSIX rename
    # of a regular file onto a non-empty directory is an error). The helper
    # MUST still remove the .tmp afterwards before re-raising.
    path = Path.join(tmpdir, "anchor.json")
    File.mkdir_p!(path)
    File.touch!(Path.join(path, "hold"))  # make the directory non-empty

    assert_raise File.RenameError, fn ->
      AtomicWrite.write(path, "anything")
    end

    refute File.exists?(path <> ".tmp"),
           "atomic write must remove the orphan .tmp on failure (B1 reviewer fold)"
  end

  test "subsequent writes overwrite cleanly without orphan accumulation", %{tmpdir: tmpdir} do
    path = Path.join(tmpdir, "anchor.json")
    :ok = AtomicWrite.write(path, "v1")
    :ok = AtomicWrite.write(path, "v2")
    :ok = AtomicWrite.write(path, "v3")

    assert File.read!(path) == "v3"
    refute File.exists?(path <> ".tmp")
  end
end
