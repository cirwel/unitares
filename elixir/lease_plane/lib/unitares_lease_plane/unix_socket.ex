defmodule UnitaresLeasePlane.UnixSocket do
  @moduledoc """
  Supervised Unix domain socket listener owner for the lease plane.

  Owns the full filesystem lifecycle of a local AF_UNIX Bandit listener:

    1. Validates `socket_path` fail-closed (absolute path, no NUL or `.`/`..`
       segments, fits `sun_path`, no symlink at target, lockfile, or existing
       parent directory leaf).
    2. Creates missing parent directories with mode `0o700`.
    3. Acquires an exclusive non-blocking kernel `flock(2)` on
       `<socket_path>.lock` (opened with `O_NOFOLLOW` and mode `0o600`) and
       holds it for the lifetime of the GenServer. The lockfile is intentionally
       left in place on shutdown so concurrent starts cannot reopen an inode
       race.
    4. Under that lock, verifies any pre-existing node at `socket_path` is a
       stale Unix domain socket (never a symlink, regular file, directory, or
       live listener) before unlinking it.
    5. Binds Bandit inside a fresh `0o700` staging directory, sets the socket
       mode to `0o600`, and atomically hard-links it into `socket_path` so the
       socket never appears at a world-accessible mode.
    6. On graceful shutdown, stops Bandit, unlinks `socket_path` only if its
       `{major_device, inode, ctime}` still matches our bound socket, and then
       releases the lifetime lock.
  """

  use GenServer

  import Bitwise

  require Logger

  # POSIX S_IFMT / S_IFSOCK constants for distinguishing AF_UNIX sockets from
  # FIFOs and other :other file types in File.lstat/2.
  @s_ifmt 0o170000
  @s_ifsock 0o140000

  # macOS sockaddr_un.sun_path is 104 bytes including the trailing NUL (Linux is
  # 108). Enforce the tighter bound everywhere so configs are portable.
  @max_sun_path_bytes 103

  @private_dir_alphabet ~c"abcdefghijklmnopqrstuvwxyz0123456789"

  @perl_lock_script """
  use strict;
  use warnings;
  use Fcntl qw(:DEFAULT :flock O_NOFOLLOW);
  $| = 1;
  my $p = shift @ARGV;
  sysopen(my $fh, $p, O_CREAT | O_RDWR | O_NOFOLLOW, 0600) or do { print "ERR:$!\\n"; exit 2 };
  chmod(0600, $fh);
  if (flock($fh, LOCK_EX | LOCK_NB)) {
    print "LOCKED\\n";
    my $line = <STDIN>;
    exit 0;
  } else {
    print "BUSY\\n";
    exit 1;
  }
  """

  @python_lock_script """
  import errno, fcntl, os, sys
  p = sys.argv[1]
  flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
  try:
      fd = os.open(p, flags, 0o600)
      os.fchmod(fd, 0o600)
      fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
  except OSError as exc:
      if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
          print("BUSY", flush=True)
          sys.exit(1)
      print(f"ERR:{exc}", flush=True)
      sys.exit(2)
  print("LOCKED", flush=True)
  sys.stdin.readline()
  """

  @type node_id :: {integer(), integer(), integer()}

  @spec start_link(keyword()) :: GenServer.on_start()
  def start_link(opts) when is_list(opts) do
    {gen_opts, init_opts} = Keyword.split(opts, [:name])
    GenServer.start_link(__MODULE__, init_opts, gen_opts)
  end

  @spec socket_path(GenServer.server()) :: String.t()
  def socket_path(server), do: GenServer.call(server, :socket_path)

  @doc """
  Validates the syntactic safety of a Unix domain socket path before touching
  the filesystem.
  """
  @spec validate_socket_path(term()) :: :ok | {:error, term()}
  def validate_socket_path(path) when is_binary(path) do
    cond do
      path == "" ->
        {:error, :empty_socket_path}

      String.contains?(path, <<0>>) ->
        {:error, :nul_byte_in_socket_path}

      Path.type(path) != :absolute ->
        {:error, {:relative_socket_path, path}}

      String.ends_with?(path, "/") ->
        {:error, {:trailing_slash_in_socket_path, path}}

      Enum.any?(Path.split(path), &(&1 in [".", ".."])) ->
        {:error, {:dot_segment_in_socket_path, path}}

      byte_size(path) > @max_sun_path_bytes ->
        {:error, {:socket_path_too_long, byte_size(path), @max_sun_path_bytes}}

      Path.basename(path) in ["", ".", ".."] ->
        {:error, {:invalid_socket_basename, path}}

      true ->
        :ok
    end
  end

  def validate_socket_path(other), do: {:error, {:invalid_socket_path, other}}

  @impl GenServer
  def init(opts) do
    Process.flag(:trap_exit, true)

    socket_path = Keyword.fetch!(opts, :socket_path)
    plug = Keyword.get(opts, :plug, UnitaresLeasePlane.HTTPRouter)
    bandit_options = Keyword.get(opts, :bandit_options, [])

    with :ok <- validate_socket_path(socket_path),
         :ok <- ensure_safe_parent_dir(socket_path),
         :ok <- check_target_and_lock_paths(socket_path),
         {:ok, lock_port} <- acquire_lock(socket_path <> ".lock") do
      case prepare_and_bind(socket_path, plug, bandit_options) do
        {:ok, bandit_pid, node_id} ->
          Logger.info(
            "Running #{inspect(plug)} with Bandit over Unix domain socket at #{socket_path} (mode 0600)"
          )

          {:ok,
           %{
             socket_path: socket_path,
             lock_port: lock_port,
             bandit_pid: bandit_pid,
             node_id: node_id
           }}

        {:error, reason} ->
          release_lock(lock_port)
          {:stop, reason}
      end
    else
      {:error, reason} ->
        {:stop, reason}
    end
  end

  @impl GenServer
  def handle_call(:socket_path, _from, state) do
    {:reply, state.socket_path, state}
  end

  @impl GenServer
  def handle_info({:EXIT, pid, reason}, %{bandit_pid: pid} = state) do
    {:stop, reason, %{state | bandit_pid: nil}}
  end

  def handle_info({port, {:exit_status, status}}, %{lock_port: port} = state) do
    {:stop, {:lock_lost, status}, %{state | lock_port: nil}}
  end

  def handle_info({:EXIT, port, reason}, %{lock_port: port} = state) do
    {:stop, {:lock_lost, reason}, %{state | lock_port: nil}}
  end

  def handle_info(_msg, state) do
    {:noreply, state}
  end

  @impl GenServer
  def terminate(_reason, state) do
    stop_bandit(state.bandit_pid)
    unlink_own_socket(state.socket_path, state.node_id)
    release_lock(state.lock_port)
    :ok
  end

  # ---------- Path & parent directory safety ----------

  defp ensure_safe_parent_dir(socket_path) do
    parent = Path.dirname(socket_path)

    case File.lstat(parent) do
      {:ok, %File.Stat{type: :symlink}} ->
        {:error, {:symlink_refused, parent}}

      {:ok, %File.Stat{type: :directory}} ->
        :ok

      {:ok, %File.Stat{type: other}} ->
        {:error, {:parent_not_directory, parent, other}}

      {:error, :enoent} ->
        with :ok <- mkdir_p_private(parent),
             {:ok, %File.Stat{type: :directory}} <- File.lstat(parent) do
          :ok
        else
          {:ok, %File.Stat{type: :symlink}} -> {:error, {:symlink_refused, parent}}
          {:ok, %File.Stat{type: other}} -> {:error, {:parent_not_directory, parent, other}}
          {:error, reason} -> {:error, reason}
        end

      {:error, reason} ->
        {:error, {:parent_lstat_failed, parent, reason}}
    end
  end

  defp mkdir_p_private(dir) do
    case find_missing_dirs(dir, []) do
      {:ok, missing} ->
        Enum.reduce_while(missing, :ok, fn path, :ok ->
          case File.mkdir(path) do
            :ok ->
              case File.chmod(path, 0o700) do
                :ok -> {:cont, :ok}
                {:error, reason} -> {:halt, {:error, {:parent_chmod_failed, path, reason}}}
              end

            {:error, :eexist} ->
              case File.lstat(path) do
                {:ok, %File.Stat{type: :directory}} ->
                  {:cont, :ok}

                {:ok, %File.Stat{type: :symlink}} ->
                  {:halt, {:error, {:symlink_refused, path}}}

                {:ok, %File.Stat{type: other}} ->
                  {:halt, {:error, {:parent_not_directory, path, other}}}

                {:error, reason} ->
                  {:halt, {:error, reason}}
              end

            {:error, reason} ->
              {:halt, {:error, {:parent_mkdir_failed, path, reason}}}
          end
        end)

      {:error, reason} ->
        {:error, reason}
    end
  end

  defp find_missing_dirs("/", acc), do: {:ok, acc}

  defp find_missing_dirs(dir, acc) do
    case File.lstat(dir) do
      {:error, :enoent} ->
        parent = Path.dirname(dir)

        if parent == dir do
          {:ok, [dir | acc]}
        else
          find_missing_dirs(parent, [dir | acc])
        end

      {:ok, %File.Stat{type: :directory}} ->
        {:ok, acc}

      # Allow OS-managed top-level symlinks (e.g. /tmp -> private/tmp on macOS)
      # when creating a new subdirectory beneath them, while refusing any
      # user-controlled symlink leaf.
      {:ok, %File.Stat{type: :symlink, uid: 0}} ->
        {:ok, acc}

      {:ok, %File.Stat{type: :symlink}} ->
        {:error, {:symlink_refused, dir}}

      {:ok, %File.Stat{type: other}} ->
        {:error, {:parent_not_directory, dir, other}}

      {:error, reason} ->
        {:error, {:parent_lstat_failed, dir, reason}}
    end
  end

  defp check_target_and_lock_paths(socket_path) do
    lock_path = socket_path <> ".lock"

    with :ok <- check_target_path(socket_path),
         :ok <- check_lock_path(lock_path) do
      :ok
    end
  end

  defp check_target_path(socket_path) do
    case File.lstat(socket_path) do
      {:error, :enoent} ->
        :ok

      {:ok, %File.Stat{type: :symlink}} ->
        {:error, {:symlink_refused, socket_path}}

      {:ok, %File.Stat{} = stat} ->
        if socket_stat?(stat) do
          :ok
        else
          {:error, {:not_a_socket, socket_path, stat.type}}
        end

      {:error, reason} ->
        {:error, {:target_lstat_failed, socket_path, reason}}
    end
  end

  defp check_lock_path(lock_path) do
    case File.lstat(lock_path) do
      {:error, :enoent} ->
        :ok

      {:ok, %File.Stat{type: :symlink}} ->
        {:error, {:symlink_refused, lock_path}}

      {:ok, %File.Stat{type: :regular}} ->
        :ok

      {:ok, %File.Stat{type: other}} ->
        {:error, {:invalid_lock_file, lock_path, other}}

      {:error, reason} ->
        {:error, {:lock_lstat_failed, lock_path, reason}}
    end
  end

  defp socket_stat?(%File.Stat{type: :other, mode: mode}) do
    band(mode, @s_ifmt) == @s_ifsock
  end

  defp socket_stat?(_), do: false

  # ---------- Lifetime flock(2) via Port ----------

  defp acquire_lock(lock_path) do
    case lock_helper_command(lock_path) do
      {:ok, executable, args} ->
        port =
          Port.open({:spawn_executable, executable}, [
            :binary,
            :exit_status,
            {:line, 512},
            :use_stdio,
            args: args
          ])

        wait_for_lock_status(port)

      {:error, reason} ->
        {:error, reason}
    end
  end

  defp lock_helper_command(lock_path) do
    cond do
      perl = System.find_executable("perl") ->
        {:ok, perl, ["-e", @perl_lock_script, lock_path]}

      python = System.find_executable("python3") ->
        {:ok, python, ["-c", @python_lock_script, lock_path]}

      true ->
        {:error, :flock_helper_unavailable}
    end
  end

  defp wait_for_lock_status(port) do
    receive do
      {^port, {:data, {:eol, "LOCKED"}}} ->
        {:ok, port}

      {^port, {:data, {:eol, "BUSY"}}} ->
        drain_port_exit(port)
        {:error, :eaddrinuse}

      {^port, {:data, {:eol, "ERR:" <> msg}}} ->
        drain_port_exit(port)
        {:error, {:lock_error, msg}}

      {^port, {:exit_status, status}} ->
        {:error, {:lock_helper_exited, status}}
    after
      5_000 ->
        release_lock(port)
        {:error, :lock_timeout}
    end
  end

  defp drain_port_exit(port) do
    receive do
      {^port, {:exit_status, _}} -> :ok
      {:EXIT, ^port, _} -> :ok
    after
      500 ->
        safe_close_port(port)
        :ok
    end
  end

  defp release_lock(nil), do: :ok

  defp release_lock(port) when is_port(port) do
    if Port.info(port) != nil do
      try do
        Port.command(port, "UNLOCK\n")
      rescue
        ArgumentError -> :ok
      end

      receive do
        {^port, {:exit_status, _}} -> :ok
        {:EXIT, ^port, _} -> :ok
      after
        1_000 ->
          safe_close_port(port)
      end
    end

    :ok
  end

  defp safe_close_port(port) do
    if Port.info(port) != nil do
      try do
        Port.close(port)
      rescue
        ArgumentError -> :ok
      end
    end

    :ok
  end

  # ---------- Stale socket cleanup & race-free 0600 bind ----------

  defp prepare_and_bind(socket_path, plug, bandit_options) do
    with :ok <- clear_stale_socket(socket_path) do
      bind_into_place(socket_path, plug, bandit_options)
    end
  end

  defp clear_stale_socket(socket_path) do
    case File.lstat(socket_path) do
      {:error, :enoent} ->
        :ok

      {:ok, %File.Stat{type: :symlink}} ->
        {:error, {:symlink_refused, socket_path}}

      {:ok, %File.Stat{} = stat} ->
        if socket_stat?(stat) do
          probe_and_unlink_stale(socket_path)
        else
          {:error, {:not_a_socket, socket_path, stat.type}}
        end

      {:error, reason} ->
        {:error, {:target_lstat_failed, socket_path, reason}}
    end
  end

  defp probe_and_unlink_stale(socket_path) do
    case :gen_tcp.connect({:local, socket_path}, 0, [:binary, active: false], 1_000) do
      {:ok, sock} ->
        :gen_tcp.close(sock)
        {:error, :eaddrinuse}

      {:error, reason} when reason in [:econnrefused, :enoent] ->
        case File.rm(socket_path) do
          :ok -> :ok
          {:error, :enoent} -> :ok
          {:error, rm_reason} -> {:error, {:stale_unlink_failed, socket_path, rm_reason}}
        end

      {:error, reason} ->
        {:error, {:stale_probe_failed, socket_path, reason}}
    end
  end

  defp bind_into_place(socket_path, plug, bandit_options) do
    parent = Path.dirname(socket_path)
    basename = Path.basename(socket_path)

    with {:ok, private_dir} <- make_private_dir(parent, byte_size(basename)) do
      staged = Path.join(private_dir, "s")

      if byte_size(staged) > @max_sun_path_bytes do
        remove_private_dir(private_dir, staged)
        {:error, {:staged_socket_path_too_long, byte_size(staged), @max_sun_path_bytes}}
      else
        bandit_opts =
          bandit_options
          |> Keyword.drop([:ip, :port, :plug])
          |> Keyword.merge(
            plug: plug,
            ip: {:local, staged},
            port: 0,
            startup_log: false
          )

        try do
          with {:ok, bandit_pid} <- Bandit.start_link(bandit_opts),
               :ok <- tighten_and_link(bandit_pid, staged, socket_path, private_dir),
               {:ok, node_id} <- verify_bound_socket(socket_path, bandit_pid) do
            {:ok, bandit_pid, node_id}
          else
            {:error, reason} -> {:error, reason}
          end
        after
          remove_private_dir(private_dir, staged)
        end
      end
    end
  end

  defp tighten_and_link(bandit_pid, staged, socket_path, private_dir) do
    with :ok <- File.chmod(staged, 0o600),
         {:ok, staged_stat} <- File.lstat(staged, time: :posix),
         :ok <- link_staged_socket(staged, socket_path),
         :ok <- remove_private_dir(private_dir, staged),
         {:ok, bound_stat} <- File.lstat(socket_path, time: :posix),
         :ok <- verify_same_inode(staged_stat, bound_stat) do
      :ok
    else
      {:error, reason} ->
        stop_bandit(bandit_pid)
        {:error, reason}
    end
  end

  defp link_staged_socket(staged, socket_path) do
    case :file.make_link(String.to_charlist(staged), String.to_charlist(socket_path)) do
      :ok -> :ok
      {:error, :eexist} -> {:error, :eaddrinuse}
      {:error, reason} -> {:error, {:link_failed, socket_path, reason}}
    end
  end

  defp verify_same_inode(staged_stat, bound_stat) do
    if {bound_stat.major_device, bound_stat.inode} ==
         {staged_stat.major_device, staged_stat.inode} do
      :ok
    else
      {:error, :eaddrinuse}
    end
  end

  defp verify_bound_socket(socket_path, bandit_pid) do
    case File.lstat(socket_path, time: :posix) do
      {:ok, %File.Stat{} = stat} ->
        mode = band(stat.mode, 0o777)

        cond do
          not socket_stat?(stat) ->
            stop_bandit(bandit_pid)
            {:error, {:not_a_socket, socket_path, stat.type}}

          mode != 0o600 ->
            stop_bandit(bandit_pid)
            {:error, {:unsafe_socket_mode, socket_path, mode}}

          true ->
            {:ok, node_id_from_stat(stat)}
        end

      {:error, reason} ->
        stop_bandit(bandit_pid)
        {:error, {:bound_lstat_failed, socket_path, reason}}
    end
  end

  defp make_private_dir(parent, basename_len) do
    name_len = min(12, max(1, basename_len - 3))
    make_private_dir_attempt(parent, name_len, 32)
  end

  defp make_private_dir_attempt(parent, _name_len, 0) do
    {:error, {:private_dir_exhausted, parent}}
  end

  defp make_private_dir_attempt(parent, name_len, attempts) do
    dir = Path.join(parent, "." <> random_token(name_len))

    case File.mkdir(dir) do
      :ok ->
        case File.chmod(dir, 0o700) do
          :ok ->
            {:ok, dir}

          {:error, reason} ->
            _ = File.rmdir(dir)
            {:error, {:private_dir_chmod_failed, dir, reason}}
        end

      {:error, :eexist} ->
        make_private_dir_attempt(parent, name_len, attempts - 1)

      {:error, reason} ->
        {:error, {:private_dir_mkdir_failed, dir, reason}}
    end
  end

  defp random_token(len) do
    alphabet_size = length(@private_dir_alphabet)

    for <<byte <- :crypto.strong_rand_bytes(len)>>, into: "" do
      <<Enum.at(@private_dir_alphabet, rem(byte, alphabet_size))>>
    end
  end

  defp remove_private_dir(private_dir, staged) do
    _ = File.rm(staged)
    _ = File.rmdir(private_dir)
    :ok
  end

  defp node_id_from_stat(%File.Stat{major_device: dev, inode: ino, ctime: ctime}) do
    {dev, ino, ctime}
  end

  defp stop_bandit(nil), do: :ok

  defp stop_bandit(pid) when is_pid(pid) do
    if Process.alive?(pid) do
      try do
        Supervisor.stop(pid, :shutdown, 5_000)
      catch
        :exit, _ -> :ok
      end
    end

    :ok
  end

  defp unlink_own_socket(socket_path, expected_node_id) do
    case File.lstat(socket_path, time: :posix) do
      {:ok, %File.Stat{} = stat} ->
        if node_id_from_stat(stat) == expected_node_id do
          _ = File.rm(socket_path)
        end

        :ok

      {:error, _} ->
        :ok
    end
  end
end
