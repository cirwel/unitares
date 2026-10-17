defmodule AgentOrchestrator.NodeDistribution do
  @moduledoc """
  Names the node at runtime with a cookie that never appears on a command line.

  Passing the cookie to `elixir --cookie` (or through `-args_file` or
  `ERL_AFLAGS`, which the `erl` launcher expands back into the emulator's
  arguments) puts it in the argv of `beam.smp`, where any process listing can
  read it. Node access is arbitrary code execution, and a listing is how this
  cookie reached an agent transcript on 2026-10-15.

  Erlang's `auth` reads `.erlang.cookie` from the emulator's boot-time home
  when distribution starts. So `scripts/start.sh` writes the cookie to a
  private file (directory 0700, file 0400), launches the VM with `HOME` set to
  that directory and the real home in `AGENT_ORCHESTRATOR_NODE_REAL_HOME`, and
  this module puts the real `HOME` back before anything else runs, then starts
  the named node. The node holds the private cookie from its first instant: no
  throwaway cookie and no window. Children spawned later inherit the real
  `HOME`.
  """

  require Logger

  @real_home_var "AGENT_ORCHESTRATOR_NODE_REAL_HOME"
  @sname_var "AGENT_ORCHESTRATOR_NODE_SNAME"
  @default_sname "agent-orchestrator"

  @doc """
  What to do for this environment: stay unnamed, or restore `real_home` and
  start as `sname`. Pure, so the decision is testable without starting
  distribution.
  """
  @spec plan(%{optional(String.t()) => String.t()}) ::
          :unnamed | {:named, String.t(), String.t()}
  def plan(env) do
    case Map.get(env, @real_home_var, "") do
      "" ->
        :unnamed

      real_home ->
        sname =
          case Map.get(env, @sname_var, "") do
            "" -> @default_sname
            name -> name
          end

        {:named, sname, real_home}
    end
  end

  @doc """
  Restore the real `HOME` and start distribution per `plan/1`. A failure to
  start is logged and the service keeps running unnamed: distribution is for
  live introspection, not for serving requests.
  """
  @spec start_from_env() :: :unnamed | {:named, node()} | {:error, term()}
  def start_from_env do
    case plan(System.get_env()) do
      :unnamed ->
        :unnamed

      {:named, sname, real_home} ->
        System.put_env("HOME", real_home)
        System.delete_env(@real_home_var)

        case Node.start(String.to_atom(sname), :shortnames) do
          {:ok, _} ->
            {:named, node()}

          {:error, reason} = error ->
            Logger.error(
              "[agent-orchestrator] could not start the named node #{sname}: " <>
                "#{inspect(reason)}; continuing unnamed (live remote shell disabled)"
            )

            error
        end
    end
  end
end
