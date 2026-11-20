defmodule UnitaresLeasePlane.EffectFileOps do
  @moduledoc """
  Filesystem operations for the `file_write` executor, behind a seam so the live
  commit AND every step of the in-process compensation (rollback) can be
  fault-injected in tests — the hard precondition the slice-2 dialectic review
  set for the live-write path. The default delegates to `File`; tests swap in a
  fake via `Application.put_env(:lease_plane, :effect_file_ops, FakeFileOps)`.

  ## Symbolic links are refused at use time

  The executor receives the canonical path of a leased `file://` surface.
  Canonicalization resolves every existing component with `realpath`, so no
  component of that path is a symbolic link when the lease is granted. A link
  found here was planted afterwards, and following it would read, write or
  remove a file the lease never named. Every operation therefore `lstat`s each
  existing component first and refuses with `{:error, :symlink_in_path}`.

  A new file is created with `O_CREAT|O_EXCL` (`:exclusive`), which fails on
  any directory entry already present, a dangling link included, so a link
  planted between the check and the create is not followed. Overwriting an
  existing regular file still goes through `File.write/2`; a swap of that file
  for a link between the check and the open is a narrower window this module
  does not close (closing it needs `openat`/`O_NOFOLLOW`, which `:file` does
  not expose).
  """

  @callback read(path :: String.t()) :: {:ok, binary()} | {:error, term()}
  @callback write(path :: String.t(), bytes :: binary()) :: :ok | {:error, term()}
  @callback rm(path :: String.t()) :: :ok | {:error, term()}

  @behaviour __MODULE__

  @impl true
  def read(path) do
    with {:ok, _leaf} <- lstat_without_symlinks(path), do: File.read(path)
  end

  @impl true
  def write(path, bytes) do
    case lstat_without_symlinks(path) do
      {:ok, :absent} -> create_exclusive(path, bytes)
      {:ok, %File.Stat{}} -> File.write(path, bytes)
      {:error, _} = error -> error
    end
  end

  @impl true
  def rm(path) do
    with {:ok, _leaf} <- lstat_without_symlinks(path), do: File.rm(path)
  end

  @doc """
  `:ok` when no existing component of `path` is a symbolic link, otherwise
  `{:error, :symlink_in_path}` (or the `lstat` error that stopped the walk).
  """
  @spec check_no_symlinks(String.t()) :: :ok | {:error, term()}
  def check_no_symlinks(path) do
    case lstat_without_symlinks(path) do
      {:ok, _leaf} -> :ok
      {:error, _} = error -> error
    end
  end

  # Walk the components from the root. Returns the leaf's stat, `:absent` once a
  # component does not exist (nothing below it can exist either), or an error.
  defp lstat_without_symlinks(path) when is_binary(path) do
    path
    |> Path.split()
    |> Enum.reduce_while({nil, {:ok, :absent}}, fn part, {prefix, _} ->
      current = if prefix, do: Path.join(prefix, part), else: part

      case File.lstat(current) do
        {:ok, %File.Stat{type: :symlink}} -> {:halt, {current, {:error, :symlink_in_path}}}
        {:ok, stat} -> {:cont, {current, {:ok, stat}}}
        {:error, :enoent} -> {:halt, {current, {:ok, :absent}}}
        {:error, reason} -> {:halt, {current, {:error, reason}}}
      end
    end)
    |> elem(1)
  end

  # O_CREAT|O_EXCL: refuses any entry already at `path`, a planted link included.
  # A regular file that appeared since the check is overwritten as before; a
  # link is refused.
  defp create_exclusive(path, bytes) do
    case :file.open(path, [:write, :exclusive, :binary, :raw]) do
      {:ok, fd} ->
        try do
          :file.write(fd, bytes)
        after
          :file.close(fd)
        end

      {:error, :eexist} ->
        case File.lstat(path) do
          {:ok, %File.Stat{type: :regular}} -> File.write(path, bytes)
          {:ok, %File.Stat{type: :symlink}} -> {:error, :symlink_in_path}
          {:ok, _other} -> {:error, :eexist}
          {:error, _} = error -> error
        end

      {:error, _} = error ->
        error
    end
  end
end
