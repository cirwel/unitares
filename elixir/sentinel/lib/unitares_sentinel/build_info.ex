defmodule UnitaresSentinel.BuildInfo do
  @moduledoc """
  Reports the git commit + app version the running BEAM Sentinel booted from.

  Answers "is the merged fix actually live?" from the alert stream instead of
  an SSH-and-`git log` guess — the running-process-vs-master-commit drift class
  that has repeatedly masked deployed-but-not-running fixes (the lease-plane
  #568 incident, the forced-release-alarm confusion this module follows from).

  The SHA is read at RUNTIME (boot), not compile time, and on purpose: the
  Sentinel runs via `mix run`, which recompiles changed modules on boot, but a
  compile-time attribute in THIS module would only refresh when this file
  itself changed — going stale whenever a commit touched other files. A runtime
  `git rev-parse` in the node's working directory always reflects the actual
  checkout being executed. Result is cached in `:persistent_term` after first
  read (git is shelled out once per boot, never on a hot path).
  """

  @cache_key {__MODULE__, :info}

  @type t :: %{
          version: String.t(),
          sha: String.t(),
          dirty: boolean(),
          summary: String.t()
        }

  @doc "Cached build info for this booted node."
  @spec info() :: t()
  def info do
    case :persistent_term.get(@cache_key, nil) do
      nil ->
        detected = detect()
        :persistent_term.put(@cache_key, detected)
        detected

      cached ->
        cached
    end
  end

  @spec version() :: String.t()
  def version, do: info().version

  @spec sha() :: String.t()
  def sha, do: info().sha

  @spec summary() :: String.t()
  def summary, do: info().summary

  @doc false
  # Uncached probe — `info/0` is the caller-facing memoized entry point.
  @spec detect() :: t()
  def detect do
    version = app_version()
    sha = git_sha()
    dirty = git_dirty?()
    suffix = if dirty, do: " (dirty)", else: ""

    %{
      version: version,
      sha: sha,
      dirty: dirty,
      summary: "unitares_sentinel #{version} @#{sha}#{suffix}"
    }
  end

  defp app_version do
    case Application.spec(:unitares_sentinel, :vsn) do
      vsn when is_list(vsn) -> List.to_string(vsn)
      _ -> "unknown"
    end
  end

  defp git_sha do
    case run_git(["rev-parse", "--short=12", "HEAD"]) do
      {:ok, out} when out != "" -> out
      _ -> "unknown"
    end
  end

  defp git_dirty? do
    case run_git(["status", "--porcelain"]) do
      {:ok, ""} -> false
      {:ok, _changes} -> true
      :error -> false
    end
  end

  # Best-effort: git missing from PATH, a non-repo cwd, or any error degrades to
  # :error (→ "unknown" sha / not-dirty). Never raises into the boot path.
  defp run_git(args) do
    case System.cmd("git", args, stderr_to_stdout: true) do
      {out, 0} -> {:ok, String.trim(out)}
      _ -> :error
    end
  rescue
    _ -> :error
  catch
    _, _ -> :error
  end
end
