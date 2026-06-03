# Live smoke: spawn a lease-bound ephemeral agent against the running lease
# plane (127.0.0.1:8788) using the REAL LeasePlaneClient, prove acquire-on-spawn
# and release-on-exit end to end.
#
#   source ~/.config/cirwel/secrets.env
#   LEASE_PLANE_BEARER_TOKEN=$LEASE_PLANE_BEARER_TOKEN mix run scripts/live_smoke.exs
#
# Acquires a remote_heartbeat lease on a novel `agent:<id>` surface (pure DB TTL
# row, reaper-reaped — self-heals even if this script is killed mid-run).

require Logger

bearer = Application.get_env(:agent_orchestrator, :lease_plane_bearer_token)

if is_nil(bearer) or bearer == "" do
  IO.puts("\n  SKIP: LEASE_PLANE_BEARER_TOKEN not set — cannot hit the live plane.\n")
  System.halt(0)
end

# `resident:/` is a STAND-IN scheme: the plane has no `agent:` scheme yet (the
# real follow-up this slice surfaced). Using a valid scheme here proves the live
# acquire→release HTTP/auth/lifecycle path end to end.
surface = "resident:/ephemeral-agent-smoke-#{System.system_time(:second)}"

{:ok, id, _pid} =
  AgentOrchestrator.run(%{
    cmd: "sh",
    args: ["-c", "echo 'ephemeral agent reporting in'; sleep 1; echo done"],
    lease: %{required: true, surface_id: surface}
  })

IO.puts("\n  spawned lease-bound ephemeral agent: #{id}")

case AgentOrchestrator.await(id, 10_000) do
  {:ok, result} ->
    IO.puts("  exit_status   : #{result.exit_status}")
    IO.puts("  lease_id      : #{result.lease_id}")
    IO.puts("  lease_released: #{result.lease_released}")
    IO.puts("  output        : #{inspect(result.output)}")

    cond do
      is_nil(result.lease_id) ->
        IO.puts("\n  ✗ FAIL: no lease_id — plane did not grant a lease\n")
        System.halt(1)

      result.exit_status != 0 ->
        IO.puts("\n  ✗ FAIL: non-zero exit\n")
        System.halt(1)

      not result.lease_released ->
        IO.puts("\n  ✗ FAIL: lease #{result.lease_id} was NOT released (orphan until TTL)\n")
        System.halt(1)

      true ->
        IO.puts("\n  ✓ live acquire→run→release round-trip OK (lease #{result.lease_id} released)\n")
    end

  {:error, reason} ->
    IO.puts("\n  ✗ FAIL: await returned #{inspect(reason)}\n")
    System.halt(1)
end
