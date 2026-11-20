defmodule UnitaresLeasePlane.EffectFileOpsTest do
  @moduledoc """
  The real filesystem seam refuses to follow a symbolic link planted in a
  leased path after canonicalization: the write must land on the leased path
  or not at all.
  """
  use ExUnit.Case, async: true

  alias UnitaresLeasePlane.EffectFileOps

  # ExUnit's tmp_dir sits under the checkout, so no component is a link.
  @moduletag :tmp_dir

  test "creates a new regular file and overwrites an existing one", %{tmp_dir: dir} do
    path = Path.join(dir, "note.txt")

    assert :ok = EffectFileOps.write(path, "first\n")
    assert {:ok, "first\n"} = EffectFileOps.read(path)
    assert :ok = EffectFileOps.write(path, "second\n")
    assert File.read!(path) == "second\n"
    assert :ok = EffectFileOps.rm(path)
    refute File.exists?(path)
  end

  test "write through a dangling leaf link is refused and the target is not created", %{
    tmp_dir: dir
  } do
    target = Path.join(dir, "outside.txt")
    link = Path.join(dir, "out.txt")
    File.ln_s!(target, link)

    assert {:error, :symlink_in_path} = EffectFileOps.write(link, "x")
    refute File.exists?(target)
  end

  test "write through a live leaf link is refused and the target is unchanged", %{tmp_dir: dir} do
    target = Path.join(dir, "outside.txt")
    File.write!(target, "keep\n")
    link = Path.join(dir, "out.txt")
    File.ln_s!(target, link)

    assert {:error, :symlink_in_path} = EffectFileOps.write(link, "x")
    assert File.read!(target) == "keep\n"
  end

  test "a parent directory replaced by a link is refused", %{tmp_dir: dir} do
    real_dir = Path.join(dir, "elsewhere")
    File.mkdir_p!(real_dir)
    linked_parent = Path.join(dir, "work")
    File.ln_s!(real_dir, linked_parent)

    assert {:error, :symlink_in_path} =
             EffectFileOps.write(Path.join(linked_parent, "out.txt"), "x")

    refute File.exists?(Path.join(real_dir, "out.txt"))
  end

  test "read and rm refuse a leaf link", %{tmp_dir: dir} do
    target = Path.join(dir, "outside.txt")
    File.write!(target, "secret\n")
    link = Path.join(dir, "out.txt")
    File.ln_s!(target, link)

    assert {:error, :symlink_in_path} = EffectFileOps.read(link)
    assert {:error, :symlink_in_path} = EffectFileOps.rm(link)
    assert File.read!(target) == "secret\n"
    assert {:ok, %File.Stat{type: :symlink}} = File.lstat(link)
  end

  test "absent paths keep their errors", %{tmp_dir: dir} do
    missing = Path.join(dir, "missing.txt")

    assert {:error, :enoent} = EffectFileOps.read(missing)
    assert {:error, :enoent} = EffectFileOps.rm(missing)
    assert {:error, :enoent} = EffectFileOps.write(Path.join(dir, "no/such/dir.txt"), "x")
  end

  test "a hard-linked leaf is refused and the other name's file is unchanged", %{tmp_dir: dir} do
    other = Path.join(dir, "operator-owned.txt")
    File.write!(other, "keep\n")
    leased = Path.join(dir, "out.txt")
    File.ln!(other, leased)

    assert {:error, :hardlinked_leaf} = EffectFileOps.write(leased, "x")
    assert {:error, :hardlinked_leaf} = EffectFileOps.read(leased)
    assert File.read!(other) == "keep\n"
  end

  test "a non-regular leaf is refused without opening it", %{tmp_dir: dir} do
    fifo = Path.join(dir, "out.txt")
    {_, 0} = System.cmd("mkfifo", [fifo])
    # tmp_dir is kept after the run; a FIFO left there blocks any tool that
    # later opens every file under the checkout.
    on_exit(fn -> File.rm(fifo) end)

    # Opening a FIFO for reading blocks until a writer appears; a refusal that
    # returns at all shows the leaf was never opened.
    task = Task.async(fn -> {EffectFileOps.read(fifo), EffectFileOps.write(fifo, "x")} end)

    assert {{:error, :not_regular_file}, {:error, :not_regular_file}} =
             Task.await(task, 5_000)
  end

  # The tests below change the tree after the helper has walked to (and holds)
  # the parent directory, and before it touches the leaf: the window between a
  # check by path and an open by path that `:file` alone cannot close.

  test "a leaf swapped for a dangling link after the walk creates nothing", %{tmp_dir: dir} do
    leased = Path.join(dir, "out.txt")
    File.write!(leased, "leased\n")
    target = Path.join(dir, "outside.txt")

    swap = fn ->
      File.rm!(leased)
      File.ln_s!(target, leased)
    end

    assert {:error, :symlink_in_path} = EffectFileOps.run_helper("write", leased, "x", swap)
    refute File.exists?(target)
  end

  test "a leaf swapped for a live link after the walk is not written through", %{tmp_dir: dir} do
    leased = Path.join(dir, "out.txt")
    File.write!(leased, "leased\n")
    other = Path.join(dir, "operator-owned.txt")
    File.write!(other, "keep\n")

    swap = fn ->
      File.rm!(leased)
      File.ln_s!(other, leased)
    end

    assert {:error, :symlink_in_path} = EffectFileOps.run_helper("write", leased, "x", swap)
    assert File.read!(other) == "keep\n"

    assert {:error, :symlink_in_path} =
             EffectFileOps.run_helper("read", leased, "", fn -> :ok end)
  end

  test "a leaf swapped for a FIFO after the walk is refused without blocking", %{tmp_dir: dir} do
    leased = Path.join(dir, "out.txt")
    File.write!(leased, "leased\n")
    on_exit(fn -> File.rm(leased) end)

    swap = fn ->
      File.rm!(leased)
      {_, 0} = System.cmd("mkfifo", [leased])
    end

    task = Task.async(fn -> EffectFileOps.run_helper("read", leased, "", swap) end)
    assert {:error, :not_regular_file} = Task.await(task, 5_000)
  end

  # A parent replaced by a link after the walk: `elsewhere` must never be
  # touched. The operation stays in the directory the walk opened, which the
  # rename moved to `work.old`.
  defp swap_parent(dir) do
    work = Path.join(dir, "work")
    elsewhere = Path.join(dir, "elsewhere")
    File.mkdir_p!(elsewhere)
    File.write!(Path.join(elsewhere, "out.txt"), "keep\n")

    fn ->
      File.rename!(work, Path.join(dir, "work.old"))
      File.ln_s!(elsewhere, work)
    end
  end

  test "a new file is created in the walked directory, not through a swapped parent", %{
    tmp_dir: dir
  } do
    File.mkdir_p!(Path.join(dir, "work"))
    leased = Path.join(dir, "work/new.txt")

    assert :ok = EffectFileOps.run_helper("write", leased, "payload", swap_parent(dir)) |> ok()
    refute File.exists?(Path.join(dir, "elsewhere/new.txt"))
    assert File.read!(Path.join(dir, "work.old/new.txt")) == "payload"
  end

  test "an overwrite lands in the walked directory, not through a swapped parent", %{
    tmp_dir: dir
  } do
    File.mkdir_p!(Path.join(dir, "work"))
    leased = Path.join(dir, "work/out.txt")
    File.write!(leased, "leased\n")

    assert :ok = EffectFileOps.run_helper("write", leased, "x", swap_parent(dir)) |> ok()
    assert File.read!(Path.join(dir, "elsewhere/out.txt")) == "keep\n"
    assert File.read!(Path.join(dir, "work.old/out.txt")) == "x"
  end

  test "rm unlinks in the walked directory, not through a swapped parent", %{tmp_dir: dir} do
    File.mkdir_p!(Path.join(dir, "work"))
    leased = Path.join(dir, "work/out.txt")
    File.write!(leased, "leased\n")

    assert :ok = EffectFileOps.run_helper("rm", leased, "", swap_parent(dir)) |> ok()
    assert File.read!(Path.join(dir, "elsewhere/out.txt")) == "keep\n"
    refute File.exists?(Path.join(dir, "work.old/out.txt"))
  end

  defp ok({:ok, _}), do: :ok
  defp ok(other), do: other

  test "a nil pre-image is refused and creates nothing", %{tmp_dir: dir} do
    path = Path.join(dir, "out.txt")

    assert {:error, :no_pre_image} = EffectFileOps.write(path, nil)
    refute File.exists?(path)
  end
end
