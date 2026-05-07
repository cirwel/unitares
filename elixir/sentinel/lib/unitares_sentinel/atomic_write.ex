defmodule UnitaresSentinel.AtomicWrite do
  @moduledoc """
  Atomic file write with mode-0600 permissions and orphan-tmp cleanup.

  Binding contract per the v0.1.1 council fold (B1 reviewer): the Python
  helper at `agents/sdk/src/unitares_sdk/utils.py:atomic_write` (used for
  `~/.unitares/anchors/sentinel.json`) creates with 0o600 via
  `tempfile.mkstemp` + `os.fchmod` + `os.replace`. Naive `File.write/2`
  + `File.rename/2` would inherit the launchd umask (typically 022 →
  0o644), regressing security on a credential-bearing cursor file. This
  helper preserves the 0o600 invariant on the BEAM side.

  Cleanup discipline: the `.tmp` file is removed in the `rescue` clause
  if any step (write/chmod/rename) fails. Without this, partial writes
  leave orphan `<path>.tmp` files that pile up across crash-loop restarts.

  fsync is not called on either side (Python or here) — NIT-level on
  macOS APFS and called out only so a future BLOCK doesn't surprise.
  """

  @doc """
  Write `content` to `path` atomically with mode 0o600.

  Implementation: write to `path <> ".tmp"`, chmod 0o600, then atomic
  rename. On any failure, remove the orphan tmp and re-raise.
  """
  @spec write(Path.t(), iodata()) :: :ok
  def write(path, content) do
    tmp = path <> ".tmp"

    try do
      :ok = File.write!(tmp, content)
      :ok = File.chmod!(tmp, 0o600)
      :ok = File.rename!(tmp, path)
    rescue
      e ->
        _ = File.rm(tmp)
        reraise e, __STACKTRACE__
    end
  end
end
