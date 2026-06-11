#!/usr/bin/env bash
# Hot-code-reload helper for the Surface Lease Plane (BEAM).
#
# Recompiles the lease_plane app in THIS working tree and swaps the named
# modules into the *running* node in place — no restart, no dropped leases.
# This is the operational realization of the running-process-vs-master-commit
# fix: after `git pull` in the deploy worktree, run this to make the live node
# actually run the new code.
#
#   scripts/ops/hot-reload.sh UnitaresLeasePlane.HTTPRouter [More.Modules ...]
#   scripts/ops/hot-reload.sh --changed     # reload every module whose loaded
#                                           # md5 differs from the on-disk beam
#
# --changed compares GROUND TRUTH: the md5 of each module loaded in the node
# vs the md5 of the beam on disk after compile. It deliberately does NOT use
# "what did this invocation's compile rewrite" — that mtime heuristic was
# defeated 2026-06-11 when something else compiled the tree between the
# deploy pull and the reload, so `mix compile` no-oped, the marker matched
# nothing, and the script reported "node already runs current code" while the
# node demonstrably ran the OLD code (PR #614's forwarder fix had to be
# loaded by naming the modules explicitly). Modules present on disk but not
# loaded in the node are skipped: the node's code path points at this ebin,
# so they load the current disk version on first use anyway.
#
# Requires:
#   - LEASE_PLANE_NODE_COOKIE in ~/.config/cirwel/secrets.env
#   - the node started named (start.sh with the cookie present)
#
# SCOPE — what reloads cleanly vs. what does not:
#   Stateless modules (HTTPRouter, plugs, Canonicalize, pure logic) reload
#   cleanly: requests are served by transient processes that pick up new code
#   on the next call. Long-lived *stateful* GenServers (LeaseHolder,
#   HandoffServer, the periodic workers) are the relup / code_change frontier:
#   `:code.purge` KILLS any process still executing the old module, and a
#   changed state shape needs `code_change/3`. Reload those only when you know
#   the state shape is unchanged — otherwise do a full restart for that change.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# scripts/ops/hot-reload.sh -> lease_plane root is two levels up
LEASE_PLANE_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
SECRETS_FILE="$HOME/.config/cirwel/secrets.env"
EBIN="$LEASE_PLANE_DIR/_build/dev/lib/lease_plane/ebin"

if [[ -f "$SECRETS_FILE" ]]; then
    # shellcheck disable=SC1090
    set -a; source "$SECRETS_FILE"; set +a
fi
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

if [[ -z "${LEASE_PLANE_NODE_COOKIE:-}" ]]; then
    echo "[hot-reload] LEASE_PLANE_NODE_COOKIE unset — the node is not named; cannot reload. Set it in $SECRETS_FILE and restart the plane once." >&2
    exit 1
fi

NODE_SNAME="${LEASE_PLANE_NODE_SNAME:-unitares-lease-plane}"
TARGET_NODE="${NODE_SNAME}@$(hostname -s)"
export ERL_EPMD_ADDRESS="127.0.0.1"

cd "$LEASE_PLANE_DIR"

declare -a MODS=()
HR_MODE="explicit"
if [[ "${1:-}" == "--changed" ]]; then
    HR_MODE="changed"
    mix compile >/dev/null
else
    if [[ $# -lt 1 ]]; then
        echo "usage: hot-reload.sh <Module> [Module ...] | --changed" >&2
        exit 2
    fi
    mix compile >/dev/null
    for m in "$@"; do MODS+=("Elixir.$m"); done
fi

CTRL='
node = System.get_env("HR_NODE") |> String.to_atom()
case Node.ping(node) do
  :pong -> :ok
  :pang ->
    IO.puts(:stderr, "[hot-reload] cannot reach #{inspect(node)} — node named + cookie correct?")
    System.halt(1)
end

mods =
  case System.get_env("HR_MODE") do
    "changed" ->
      # Ground truth: a module needs reloading iff the md5 the node is
      # RUNNING differs from the md5 of the beam ON DISK. Immune to who
      # compiled the tree, and when.
      ebin = System.get_env("HR_EBIN")

      Path.wildcard(Path.join(ebin, "*.beam"))
      |> Enum.flat_map(fn beam ->
        with {:ok, {mod, disk_md5}} <- :beam_lib.md5(String.to_charlist(beam)),
             {:file, _} <- :rpc.call(node, :code, :is_loaded, [mod]),
             loaded_md5 when is_binary(loaded_md5) <-
               :rpc.call(node, mod, :module_info, [:md5]),
             true <- loaded_md5 != disk_md5 do
          [mod]
        else
          # not loaded in the node (loads fresh from disk on first use),
          # already current, or beam unreadable — nothing to swap
          _ -> []
        end
      end)

    _ ->
      Enum.map(System.argv(), &String.to_atom/1)
  end

if mods == [] do
  IO.puts("[hot-reload] node matches disk beams (md5-verified) — nothing to reload.")
  System.halt(0)
end

Enum.each(mods, fn m ->
  :rpc.call(node, :code, :purge, [m])
  case :rpc.call(node, :code, :load_file, [m]) do
    {:module, ^m} -> IO.puts("[hot-reload] ok   #{inspect(m)}")
    other ->
      IO.puts(:stderr, "[hot-reload] FAIL #{inspect(m)} -> #{inspect(other)}")
      System.halt(1)
  end
end)
IO.puts("[hot-reload] reloaded #{length(mods)} module(s) on #{inspect(node)} — no restart")
'

HR_NODE="$TARGET_NODE" HR_MODE="$HR_MODE" HR_EBIN="$EBIN" exec elixir \
    --sname "hotreload$$" \
    --cookie "$LEASE_PLANE_NODE_COOKIE" \
    -e "$CTRL" -- "${MODS[@]+"${MODS[@]}"}"
