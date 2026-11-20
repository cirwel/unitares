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
  remove a file the lease never named.

  `:file` exposes neither `openat` nor `O_NOFOLLOW`, and every writable
  `:file.open/2` mode carries `O_CREAT`, so a check made by path cannot be
  carried over to an open made by path. Each operation therefore runs in a
  short-lived helper process (`perl`, from the base system on macOS and from
  Debian's essential `perl-base` in the container image) that walks the path
  one directory at a time. Each component is opened relative to the directory
  already held, with `O_NOFOLLOW|O_DIRECTORY`, and the helper changes into it
  through that descriptor. Directories are opened search-only (`O_SEARCH` on
  macOS, `O_PATH` on Linux, `O_RDONLY` where neither exists), so the walk needs
  only the search permission an open by path needs, not permission to list an
  ancestor. The leaf is then opened relative to the last
  directory, also with `O_NOFOLLOW`. No component is looked up twice, so
  nothing can be swapped between a check and a use:

    * a symbolic link at any component, the leaf included, fails the open
      (`{:error, :symlink_in_path}`); nothing is created or followed;
    * an existing leaf is opened read-write WITHOUT `O_CREAT` and without
      truncation. It is truncated and written only after `fstat` on that
      descriptor shows a regular file with a single link;
    * a missing leaf is created with `O_CREAT|O_EXCL|O_NOFOLLOW` inside the
      directory already held. An entry that appears in between is retried once
      under the overwrite rule above;
    * `rm/1` unlinks the leaf name inside the directory already held, and
      refuses a leaf that is a link;
    * opens use `O_NONBLOCK`, so a FIFO swapped in never blocks the executor.

  `read/1` and `write/2` accept only a regular file with a single link. A
  hard-linked leaf (`:hardlinked_leaf`) shares its inode with a file the lease
  does not name, and a FIFO, socket or device (`:not_regular_file`) could block
  the executor while it holds custody.

  ## What remains

  The guarantee is the one `openat` gives: bytes land in, and only in, the file
  (or, for a new file, the directory) found at the leased path when the helper
  opened it. If a process with write access to an ancestor RENAMES that
  ancestor during the operation, the directory moves and the write lands in
  it at its new name. No file outside the leased directory is created, opened
  or followed. Opening a device node swapped in at the leaf (refused
  immediately afterwards by `fstat`) can still have whatever side effect that
  device's open has. If the helper cannot be started, every operation fails
  closed with `{:error, :file_helper_unavailable}`.
  """

  @callback read(path :: String.t()) :: {:ok, binary()} | {:error, term()}
  @callback write(path :: String.t(), bytes :: binary()) :: :ok | {:error, term()}
  @callback rm(path :: String.t()) :: :ok | {:error, term()}

  @behaviour __MODULE__

  @helper_timeout_ms 60_000

  # Exit 0: success (stdout = file bytes for `read`). Exit 3: refusal, stdout =
  # "E:<token>". An optional trailing `pause` argument stops after the
  # directory walk, writes "W", and waits for one byte: a test hook that lets a
  # test change the tree between the walk and the leaf operation.
  @helper ~S"""
  use strict;
  use warnings;
  use Fcntl qw(O_RDONLY O_WRONLY O_RDWR O_CREAT O_EXCL O_NOFOLLOW O_NONBLOCK O_NOCTTY O_DIRECTORY);
  use Errno ();
  use Config ();
  $| = 1;
  binmode STDIN;
  binmode STDOUT;
  sub fail { print STDOUT "E:", $_[0]; exit 3 }
  sub fail_errno {
    fail($!{ELOOP} ? "symlink_in_path" : $!{ENOENT} ? "enoent" : $!{ENOTDIR} ? "enotdir"
       : $!{EACCES} ? "eacces" : $!{EPERM} ? "eperm" : $!{EEXIST} ? "eexist"
       : $!{EISDIR} ? "eisdir" : $!{ENOSPC} ? "enospc" : $!{EROFS} ? "erofs" : "io_error");
  }
  sub check_leaf {
    my @st = stat($_[0]) or fail_errno();
    -f _ or fail("not_regular_file");
    $st[3] == 1 or fail("hardlinked_leaf");
  }
  my ($op, $path, $len, $pause) = @ARGV;
  defined $len && $len =~ /\A[0-9]+\z/ or fail("bad_request");
  my $in = "";
  while (length($in) < $len) {
    my $n = sysread(STDIN, $in, $len - length($in), length($in));
    fail("bad_request") unless $n;
  }
  defined $path && $path =~ m{\A/} or fail("bad_request");
  my @parts = grep { length } split m{/}, $path;
  @parts or fail("not_regular_file");
  for (@parts) { fail("bad_request") if $_ eq "." || $_ eq ".." }
  my $leaf = pop @parts;
  # Reaching a file needs search permission on each directory above it, not
  # read. An O_RDONLY directory open asks for read, so a directory with mode
  # 0711 or 0311 would refuse a path that a plain open by path accepts. Open
  # directories search-only where the platform can: O_SEARCH on macOS, O_PATH
  # on Linux. Fcntl exports neither, so the values come from the system
  # headers; Linux uses another O_PATH value on alpha, hppa and sparc. A probe
  # opens "/" and changes into it the way the walk does, and falls back to
  # O_RDONLY if either step fails.
  my $dir_mode = O_RDONLY;
  {
    my $want = $^O eq "darwin" ? 0x40000000
      : $^O eq "linux" && $Config::Config{archname} !~ /\A(?:alpha|hppa|parisc|sparc)/ ? 0x200000
      : undef;
    my $probe;
    if (defined $want
        && sysopen($probe, "/", $want | O_DIRECTORY | O_NOFOLLOW | O_NONBLOCK | O_NOCTTY)
        && chdir($probe)) {
      $dir_mode = $want;
    }
    close($probe) if $probe;
  }
  sub fail_dir {
    # macOS reports a link here as ENOTDIR, Linux as ELOOP. Name it the same
    # on both; the open already failed, so this lstat only labels it.
    my $e = $! + 0;
    fail("symlink_in_path") if $!{ENOTDIR} && lstat($_[0]) && -l _;
    $! = $e;
    fail_errno();
  }
  chdir("/") or fail_errno();
  for my $p (@parts) {
    my $d;
    sysopen($d, $p, $dir_mode | O_DIRECTORY | O_NOFOLLOW | O_NONBLOCK | O_NOCTTY) or fail_dir($p);
    chdir($d) or fail_dir($p);
    close($d);
  }
  if (defined $pause && $pause eq "pause") { print STDOUT "W"; my $b; sysread(STDIN, $b, 1); }
  if ($op eq "read") {
    sysopen(my $f, $leaf, O_RDONLY | O_NOFOLLOW | O_NONBLOCK | O_NOCTTY) or fail_errno();
    check_leaf($f);
    my $data = "";
    while (1) {
      my $n = sysread($f, $data, 65536, length($data));
      defined $n or fail_errno();
      last if $n == 0;
    }
    print STDOUT $data;
    exit 0;
  }
  if ($op eq "write") {
    my $f;
    OPEN: for my $attempt (1, 2) {
      if (sysopen($f, $leaf, O_RDWR | O_NOFOLLOW | O_NONBLOCK | O_NOCTTY)) {
        check_leaf($f);
        truncate($f, 0) or fail_errno();
        last OPEN;
      }
      fail_errno() unless $!{ENOENT};
      last OPEN if sysopen($f, $leaf, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_NOCTTY, 0666);
      fail_errno() unless $!{EEXIST} && $attempt == 1;
    }
    my $off = 0;
    while ($off < length($in)) {
      my $n = syswrite($f, $in, length($in) - $off, $off);
      defined $n or fail_errno();
      $off += $n;
    }
    close($f) or fail_errno();
    exit 0;
  }
  if ($op eq "rm") {
    lstat($leaf) or fail_errno();
    -l _ and fail("symlink_in_path");
    unlink($leaf) or fail_errno();
    exit 0;
  }
  fail("bad_request");
  """

  @refusals %{
    "symlink_in_path" => :symlink_in_path,
    "enoent" => :enoent,
    "enotdir" => :enotdir,
    "eacces" => :eacces,
    "eperm" => :eperm,
    "eexist" => :eexist,
    "eisdir" => :eisdir,
    "enospc" => :enospc,
    "erofs" => :erofs,
    "not_regular_file" => :not_regular_file,
    "hardlinked_leaf" => :hardlinked_leaf,
    "bad_request" => :bad_request,
    "io_error" => :io_error
  }

  @impl true
  def read(path), do: run_helper("read", path, "")

  @impl true
  def write(path, bytes) when is_binary(bytes) do
    with {:ok, _} <- run_helper("write", path, bytes), do: :ok
  end

  # Compensation passes `nil` when the pre-image could not be read. Refuse it
  # before any open, so an absent leaf is not created empty.
  def write(_path, _bytes), do: {:error, :no_pre_image}

  @impl true
  def rm(path) do
    with {:ok, _} <- run_helper("rm", path, ""), do: :ok
  end

  @doc false
  # Runs one helper operation. `pause`, when given, is called after the helper
  # has walked and holds the parent directory, before it touches the leaf. Tests
  # only.
  @spec run_helper(String.t(), String.t(), binary(), (-> any()) | nil) ::
          {:ok, binary()} | {:error, term()}
  def run_helper(op, path, input, pause \\ nil) when is_binary(path) and is_binary(input) do
    with {:ok, perl} <- perl_path(),
         {:ok, port} <- open_port(perl, op, path, input, pause) do
      if input != "", do: send_input(port, input)
      collect(port, pause, [])
    end
  end

  defp open_port(perl, op, path, input, pause) do
    args =
      ["-e", @helper, "--", op, path, Integer.to_string(byte_size(input))] ++
        if(pause, do: ["pause"], else: [])

    port =
      Port.open({:spawn_executable, perl}, [
        :binary,
        :exit_status,
        :use_stdio,
        :hide,
        args: args,
        cd: "/",
        env: [
          {~c"PERL5OPT", false},
          {~c"PERL5LIB", false},
          {~c"PERLLIB", false},
          {~c"PERL5DB", false}
        ]
      ])

    {:ok, port}
  rescue
    # A path the OS cannot take as an argument (an embedded NUL, say).
    _ in [ArgumentError, ErlangError] -> {:error, :file_helper_unavailable}
  end

  defp send_input(port, data) do
    Port.command(port, data)
  rescue
    # The helper already exited; its status still arrives and is decoded.
    ArgumentError -> :ok
  end

  defp collect(port, pause, acc) do
    receive do
      {^port, {:data, "W" <> rest}} when is_function(pause, 0) and acc == [] ->
        pause.()
        send_input(port, "g")
        collect(port, nil, [rest])

      {^port, {:data, data}} ->
        collect(port, pause, [acc | data])

      {^port, {:exit_status, status}} ->
        decode(status, IO.iodata_to_binary(acc))
    after
      @helper_timeout_ms ->
        kill(port)
        {:error, :file_helper_timeout}
    end
  end

  defp decode(0, out), do: {:ok, out}
  defp decode(3, "E:" <> token), do: {:error, Map.get(@refusals, token, :io_error)}
  defp decode(status, _out), do: {:error, {:file_helper_exit, status}}

  defp kill(port) do
    case Port.info(port, :os_pid) do
      {:os_pid, pid} -> System.cmd("kill", ["-9", Integer.to_string(pid)], stderr_to_stdout: true)
      _ -> :ok
    end

    Port.close(port)
  rescue
    _ -> :ok
  end

  defp perl_path do
    case :persistent_term.get({__MODULE__, :perl}, nil) do
      nil ->
        found =
          if File.regular?("/usr/bin/perl"),
            do: "/usr/bin/perl",
            else: System.find_executable("perl")

        if found do
          :persistent_term.put({__MODULE__, :perl}, found)
          {:ok, found}
        else
          {:error, :file_helper_unavailable}
        end

      perl ->
        {:ok, perl}
    end
  end
end
