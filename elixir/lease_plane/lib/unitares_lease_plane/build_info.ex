defmodule UnitaresLeasePlane.BuildInfo do
  @moduledoc """
  What commit this node BOOTED from.

  Mirrors `src/versioning.py:load_build_sha_from_repo/1`: `git rev-parse
  --short HEAD` against the running code's own directory, falling back to the
  operator-supplied `UNITARES_BUILD_SHA` and finally `"unknown"`. Git wins when
  present because it cannot drift. Never raises.

  ⛔BOOT sha, deliberately — not a per-module compile sha. The lease plane is
  the one service whose pickup is `hot-reload`, so "what code is loaded" and
  "what supervision tree is running" are different questions, and only the
  second one can be answered here. `hot-reload.sh` swaps modules in a live node
  but CANNOT add a child to an already-started supervisor, so a change that
  touches `application.ex` needs a real restart. A per-module sha would advance
  on reload and report the node current while its supervision tree was still
  the old one — which is the failure this value exists to make visible.

  The consequence is that a hot-reloaded node reads STALE until it restarts,
  even though its request path is current. That is the safe direction: the
  tooling says "restart me" when a restart may be unnecessary, rather than
  saying "fine" when the tree is stale.

  Resolved once at application start and cached in app env: `/health` is the
  static, pre-auth liveness probe and must stay free of per-request work.
  """

  @app :lease_plane
  @key :build_sha

  @doc "Resolve and cache the boot sha. Called once from `Application.start/2`."
  @spec resolve!() :: String.t()
  def resolve! do
    sha = from_git() || from_env() || "unknown"
    Application.put_env(@app, @key, sha)
    sha
  end

  @doc "The cached boot sha; resolves on demand if start/2 has not run (tests)."
  @spec build_sha() :: String.t()
  def build_sha do
    case Application.get_env(@app, @key) do
      sha when is_binary(sha) and sha != "" -> sha
      _ -> resolve!()
    end
  end

  defp from_git do
    # __DIR__ is inside the running code's own tree, so this answers "which
    # checkout am I" even when cwd is elsewhere (launchd starts us from /).
    case System.cmd("git", ["-C", __DIR__, "rev-parse", "--short", "HEAD"],
           stderr_to_stdout: true
         ) do
      {out, 0} -> trimmed_or_nil(out)
      _ -> nil
    end
  rescue
    # No git binary, or a directory git refuses to read. Never fatal: a health
    # probe that crashes is worse than one that says "unknown".
    _ -> nil
  end

  defp from_env, do: trimmed_or_nil(System.get_env("UNITARES_BUILD_SHA") || "")

  defp trimmed_or_nil(value) do
    case String.trim(value) do
      "" -> nil
      trimmed -> trimmed
    end
  end
end
