defmodule AgentOrchestrator.NodeDistribution do
  @moduledoc """
  Names the node and sets its Erlang cookie at runtime, so the cookie never
  appears on a command line.

  Passing the cookie to `elixir --cookie` (or through `-args_file` or
  `ERL_AFLAGS`, which the `erl` launcher expands back into the emulator's
  arguments) puts it in the argv of `beam.smp`, where any process listing can
  read it. Node access is arbitrary code execution, and a listing is how this
  cookie reached an agent transcript on 2026-10-15. So `scripts/start.sh` only
  exports `AGENT_ORCHESTRATOR_NODE_COOKIE` into the environment, and this
  module starts distribution itself, sets the cookie, and removes the variable
  so no spawned child inherits it.

  `start.sh` also starts epmd and passes a throwaway random `-setcookie`, so
  `Node.start/2` neither depends on `~/.erlang.cookie` nor fails for want of
  epmd. Between `Node.start/2` and `Node.set_cookie/1` the node holds that
  throwaway; it is replaced before the application starts anything else.
  """

  require Logger

  @cookie_var "AGENT_ORCHESTRATOR_NODE_COOKIE"
  @sname_var "AGENT_ORCHESTRATOR_NODE_SNAME"
  @default_sname "agent-orchestrator"

  @doc """
  What to do for this environment: stay unnamed, or start as `sname` with
  `cookie`. Pure, so the decision is testable without starting distribution.
  """
  @spec plan(%{optional(String.t()) => String.t()}) ::
          :unnamed | {:named, String.t(), String.t()}
  def plan(env) do
    case Map.get(env, @cookie_var, "") do
      "" ->
        :unnamed

      cookie ->
        sname =
          case Map.get(env, @sname_var, "") do
            "" -> @default_sname
            name -> name
          end

        {:named, sname, cookie}
    end
  end

  @doc """
  Start distribution per `plan/1` and remove the cookie variable from this
  process's environment either way. A failure to start is logged and the
  service keeps running unnamed: distribution is for live introspection,
  not for serving requests.
  """
  @spec start_from_env() :: :unnamed | {:named, node()} | {:error, term()}
  def start_from_env do
    planned = plan(System.get_env())
    System.delete_env(@cookie_var)

    case planned do
      :unnamed ->
        :unnamed

      {:named, sname, cookie} ->
        case Node.start(String.to_atom(sname), :shortnames) do
          {:ok, _} ->
            Node.set_cookie(String.to_atom(cookie))
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
