defmodule UnitaresLeasePlane.EffectFileOps do
  @moduledoc """
  Filesystem operations for the `file_write` executor, behind a seam so the live
  commit AND every step of the in-process compensation (rollback) can be
  fault-injected in tests — the hard precondition the slice-2 dialectic review
  set for the live-write path. The default works on the real filesystem; tests
  swap in a fake via `Application.put_env(:lease_plane, :effect_file_ops, FakeFileOps)`.

  ## Only the leased file is touched

  The executor receives the canonical path of a leased `file://` surface.
  Canonicalization resolves every existing component with `realpath`, so no
  component of that path is a symbolic link when the lease is granted. A link
  found here was planted afterwards, and following it would read, write or
  remove a file the lease never named. Every operation therefore `lstat`s each
  existing component first and refuses with `{:error, :symlink_in_path}`.

  `read/1` and `write/2` accept only a regular file with a single link at the
  leaf. A hard-linked leaf (`:hardlinked_leaf`) shares its inode with a file the
  lease does not name, and a FIFO, socket or device (`:not_regular_file`) could
  block the executor while it holds custody.

  An existing file is opened without truncation, and its inode and device are
  compared with the `lstat` result through the open descriptor before any byte
  is read, truncated or written. A leaf or parent swapped in between resolves to
  a different inode and is refused (`:path_changed`) with the other file left
  unchanged. A new file is created with `O_CREAT|O_EXCL` (`:exclusive`), which
  fails on any entry already present, a dangling link included.

  `:file` exposes no `O_NOFOLLOW` or `openat`, so two narrow cases remain. The
  non-truncating open still carries `O_CREAT`: a leaf swapped for a dangling
  link between the check and the open can create an empty file at the link's
  target before the inode comparison refuses the write. A parent directory
  swapped between the walk and an exclusive create is not detected.
  """

  @callback read(path :: String.t()) :: {:ok, binary()} | {:error, term()}
  @callback write(path :: String.t(), bytes :: binary()) :: :ok | {:error, term()}
  @callback rm(path :: String.t()) :: :ok | {:error, term()}

  @behaviour __MODULE__

  @impl true
  def read(path) do
    case lstat_without_symlinks(path) do
      {:ok, :absent} -> {:error, :enoent}
      {:ok, %File.Stat{} = stat} -> read_if_unchanged(path, stat)
      {:error, _} = error -> error
    end
  end

  @impl true
  def write(path, bytes) when is_binary(bytes) do
    case lstat_without_symlinks(path) do
      {:ok, :absent} -> create_exclusive(path, bytes)
      {:ok, %File.Stat{} = stat} -> overwrite_if_unchanged(path, stat, bytes)
      {:error, _} = error -> error
    end
  end

  # Compensation passes `nil` when the pre-image could not be read. Refuse it
  # before any open, so an absent leaf is not created empty.
  def write(_path, _bytes), do: {:error, :no_pre_image}

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

  @doc false
  # Overwrite `path` only if the descriptor opened on it is the regular,
  # single-link file `expected` (from the component walk) describes. Public only
  # so tests can swap the leaf between the walk and the open.
  @spec overwrite_if_unchanged(String.t(), File.Stat.t(), binary()) :: :ok | {:error, term()}
  def overwrite_if_unchanged(path, %File.Stat{} = expected, bytes) do
    with :ok <- check_leaf(expected) do
      # [:read, :write] opens without O_TRUNC; nothing is truncated until the
      # descriptor is known to be the expected file.
      with_verified_fd(path, [:read, :write, :binary, :raw], expected, fn fd ->
        with {:ok, 0} <- :file.position(fd, 0),
             :ok <- :file.truncate(fd) do
          :file.write(fd, bytes)
        end
      end)
    end
  end

  defp read_if_unchanged(path, expected) do
    with :ok <- check_leaf(expected) do
      with_verified_fd(path, [:read, :binary, :raw], expected, &read_all(&1, []))
    end
  end

  defp check_leaf(%File.Stat{type: :regular, links: 1}), do: :ok
  defp check_leaf(%File.Stat{type: :regular}), do: {:error, :hardlinked_leaf}
  defp check_leaf(%File.Stat{}), do: {:error, :not_regular_file}

  defp with_verified_fd(path, modes, expected, fun) do
    case :file.open(path, modes) do
      {:ok, fd} ->
        try do
          with :ok <- same_file(fd, expected), do: fun.(fd)
        after
          :file.close(fd)
        end

      {:error, _} = error ->
        error
    end
  end

  defp same_file(fd, %File.Stat{inode: inode, major_device: device}) do
    case :file.read_file_info(fd, [{:time, :posix}]) do
      {:ok, info} ->
        case File.Stat.from_record(info) do
          %File.Stat{type: :regular, links: 1, inode: ^inode, major_device: ^device} -> :ok
          _ -> {:error, :path_changed}
        end

      {:error, _} = error ->
        error
    end
  end

  defp read_all(fd, acc) do
    case :file.read(fd, 65_536) do
      {:ok, data} -> read_all(fd, [acc | data])
      :eof -> {:ok, IO.iodata_to_binary(acc)}
      {:error, _} = error -> error
    end
  end

  # O_CREAT|O_EXCL: refuses any entry already at `path`, a planted link included.
  # An entry that appeared since the check is re-walked and overwritten only
  # under the same rules as an existing file.
  defp create_exclusive(path, bytes) do
    case :file.open(path, [:write, :exclusive, :binary, :raw]) do
      {:ok, fd} ->
        try do
          :file.write(fd, bytes)
        after
          :file.close(fd)
        end

      {:error, :eexist} ->
        case lstat_without_symlinks(path) do
          {:ok, %File.Stat{} = stat} -> overwrite_if_unchanged(path, stat, bytes)
          {:ok, :absent} -> {:error, :eexist}
          {:error, _} = error -> error
        end

      {:error, _} = error ->
        error
    end
  end
end
