defmodule AgentOrchestrator.HTTPRouter do
  @moduledoc """
  HTTP control surface for the ephemeral-agent orchestrator — the "spawn / list
  / stop agents from outside BEAM" capability deferred in
  `docs/proposals/agent-orchestrator-beam-v0.md`.

  Without this, the only way to drive the orchestrator was to BE Elixir code
  running inside the same VM. This surface lets a Python/TS/shell caller (the
  dispatch worker, a CLI, a future MCP shim) drive the same `AgentOrchestrator`
  facade over localhost HTTP.

  ## Routes

      GET    /v1/health             liveness + live-agent count
      POST   /v1/agents             spawn a supervised ephemeral agent
      GET    /v1/agents             list live agent ids
      GET    /v1/agents/:id         snapshot (captured output + status), no block
      POST   /v1/agents/:id/await   block until exit (or `timeout_ms`)
      DELETE /v1/agents/:id         stop: close the port (kill child) + release lease

  ## Trust boundary

  Bind is local-only (`127.0.0.1`, enforced in `Application`) and every route is
  bearer-gated by `AgentOrchestrator.HTTPAuth`, which fails closed. `POST
  /v1/agents` spawns an OS process, so this is an authenticated RCE surface by
  design — the same capability the in-VM `AgentOrchestrator.run/1` already has,
  exposed over one localhost trust boundary. An optional `:cmd_allowlist`
  (config, default `nil` = parity with the in-VM API) restricts which
  executables a caller may spawn.

  ## Envelope

  Every response is `{ok: boolean, ...}` JSON with a `protocol_version` field.
  Errors are typed (`schema_invalid` / `permission_denied` / `not_found` /
  `lease_denied` / `await_timeout` / `service_unavailable`) rather than leaky
  HTML — same typed-absence discipline as the lease plane router.
  """

  use Plug.Router
  use Plug.ErrorHandler

  require Logger

  @protocol_version "v0.1"

  plug(:match)

  # Auth BEFORE parsing: an unauthenticated caller must not be able to reach the
  # spawn path or probe routes by sending a body. Bearer auth reads only the
  # authorization header, so it needs no parsed body.
  plug(AgentOrchestrator.HTTPAuth)

  # SafeParsers wraps Plug.Parsers so malformed JSON / wrong media type become
  # typed 422 / 415 at one named site, rather than Bandit's default empty 400
  # (relying on Plug.ErrorHandler for that is unreliable — see SafeParsers).
  plug(AgentOrchestrator.SafeParsers,
    parsers: [:json],
    pass: ["application/json"],
    json_decoder: Jason
  )

  plug(:dispatch)

  # Backstop: anything raised inside a route handler becomes a redacted 503
  # (full error logged server-side only). Parse errors are handled upstream by
  # SafeParsers, so they never reach here.
  @impl Plug.ErrorHandler
  def handle_errors(conn, %{kind: kind, reason: reason}) do
    Logger.error("agent orchestrator HTTP error: #{Exception.format_banner(kind, reason)}")
    json(conn, 503, %{ok: false, error: "service_unavailable", reason: "internal error"})
  end

  # ---------- routes ----------

  get "/v1/health" do
    json(conn, 200, %{ok: true, status: "ok", active_agents: AgentOrchestrator.count()})
  end

  post "/v1/agents" do
    with {:ok, spec} <- build_spec(conn.body_params),
         :ok <- check_allowed(spec.cmd) do
      case AgentOrchestrator.run(spec) do
        {:ok, agent_id, _pid} ->
          json(conn, 201, %{ok: true, agent_id: agent_id})

        {:error, reason} ->
          spawn_error(conn, reason)
      end
    else
      {:error, :cmd_not_allowed, cmd} ->
        json(conn, 403, %{ok: false, error: "permission_denied", reason: "cmd not in allowlist: #{cmd}"})

      {:error, detail} ->
        json(conn, 422, %{ok: false, error: "schema_invalid", detail: detail})
    end
  end

  get "/v1/agents" do
    ids = AgentOrchestrator.list()
    json(conn, 200, %{ok: true, agents: ids, count: length(ids)})
  end

  get "/v1/agents/:id" do
    case AgentOrchestrator.snapshot(id) do
      {:ok, result} -> json(conn, 200, %{ok: true, result: present(result)})
      {:error, :not_found} -> json(conn, 404, %{ok: false, error: "not_found"})
    end
  end

  post "/v1/agents/:id/await" do
    case AgentOrchestrator.await(id, await_timeout(conn.body_params)) do
      {:ok, result} ->
        json(conn, 200, %{ok: true, result: present(result)})

      {:error, :timeout} ->
        # The await deadline passed; the agent may still be running. Distinct
        # from not_found so a caller can re-await or snapshot.
        json(conn, 504, %{ok: false, error: "await_timeout", agent_id: id})

      {:error, :not_found} ->
        json(conn, 404, %{ok: false, error: "not_found"})
    end
  end

  delete "/v1/agents/:id" do
    case AgentOrchestrator.stop(id) do
      :ok -> json(conn, 200, %{ok: true})
      {:error, :not_found} -> json(conn, 404, %{ok: false, error: "not_found"})
    end
  end

  match _ do
    json(conn, 404, %{ok: false, error: "not_found"})
  end

  # ---------- spec translation (JSON body -> AgentRunner spec) ----------

  # Translate the JSON body (string keys) into the runner spec (atom keys) via a
  # FIXED whitelist. We never `String.to_atom/1` on caller input — that is atom
  # exhaustion and is how a control surface leaks into the VM's atom table.
  defp build_spec(body) when is_map(body) do
    with {:ok, cmd} <- fetch_cmd(body),
         {:ok, args} <- fetch_args(body),
         {:ok, env} <- fetch_env(body),
         {:ok, cd} <- fetch_string(body, "cd"),
         {:ok, max_lines} <- fetch_max_lines(body),
         {:ok, max_runtime} <- fetch_max_runtime(body),
         {:ok, lease} <- fetch_lease(body),
         {:ok, lineage} <- fetch_lineage(body),
         {:ok, server_url} <- fetch_string(body, "server_url"),
         {:ok, client_session_id} <- fetch_string(body, "client_session_id") do
      spec =
        %{cmd: cmd, args: args, env: env}
        |> put_opt(:cd, cd)
        |> put_opt(:max_output_lines, max_lines)
        |> put_opt(:max_runtime_ms, max_runtime)
        |> put_opt(:lineage, lineage)
        |> put_opt(:server_url, server_url)
        |> put_opt(:client_session_id, client_session_id)
        |> put_lease(lease)

      {:ok, spec}
    end
  end

  defp build_spec(_), do: {:error, "body must be a JSON object"}

  defp fetch_cmd(body) do
    case Map.get(body, "cmd") do
      cmd when is_binary(cmd) and byte_size(cmd) > 0 -> {:ok, cmd}
      _ -> {:error, "cmd is required and must be a non-empty string"}
    end
  end

  defp fetch_args(body) do
    case Map.get(body, "args") do
      nil -> {:ok, []}
      list when is_list(list) ->
        if Enum.all?(list, &is_binary/1),
          do: {:ok, list},
          else: {:error, "args must be a list of strings"}

      _ ->
        {:error, "args must be a list of strings"}
    end
  end

  # JSON object {"KEY": "VAL"} -> [{"KEY", "VAL"}] (the Port env shape).
  defp fetch_env(body) do
    case Map.get(body, "env") do
      nil ->
        {:ok, []}

      %{} = map ->
        if Enum.all?(map, fn {k, v} -> is_binary(k) and is_binary(v) end),
          do: {:ok, Map.to_list(map)},
          else: {:error, "env must be a JSON object of string => string"}

      _ ->
        {:error, "env must be a JSON object of string => string"}
    end
  end

  defp fetch_string(body, key) do
    case Map.get(body, key) do
      nil -> {:ok, nil}
      v when is_binary(v) -> {:ok, v}
      _ -> {:error, "#{key} must be a string"}
    end
  end

  defp fetch_max_lines(body) do
    case Map.get(body, "max_output_lines") do
      nil -> {:ok, nil}
      n when is_integer(n) and n > 0 -> {:ok, n}
      _ -> {:error, "max_output_lines must be a positive integer"}
    end
  end

  # Absent => omit (runner applies its configured default ceiling). A positive
  # integer overrides the lifetime cap for this spawn. HTTP callers can raise the
  # cap for a legitimately long agent; the in-VM API additionally accepts nil to
  # disable, which is not exposed here (the control surface should stay bounded).
  defp fetch_max_runtime(body) do
    case Map.get(body, "max_runtime_ms") do
      nil -> {:ok, nil}
      n when is_integer(n) and n > 0 -> {:ok, n}
      _ -> {:error, "max_runtime_ms must be a positive integer"}
    end
  end

  # Presence/lease: absent => omit (runner default = best-effort presence);
  # `false` => opt out; object => override map (atom keys, whitelisted).
  defp fetch_lease(body) do
    case Map.get(body, "lease") do
      nil -> {:ok, :absent}
      false -> {:ok, false}
      %{} = cfg -> lease_cfg(cfg)
      _ -> {:error, "lease must be false or a JSON object"}
    end
  end

  defp lease_cfg(cfg) do
    Enum.reduce_while(
      [
        {"required", :required, &is_boolean/1, "required must be a boolean"},
        {"surface_id", :surface_id, &nonempty_string?/1, "surface_id must be a non-empty string"},
        {"holder_agent_uuid", :holder_agent_uuid, &nonempty_string?/1, "holder_agent_uuid must be a non-empty string"},
        {"ttl_s", :ttl_s, &positive_integer?/1, "ttl_s must be a positive integer"}
      ],
      {:ok, %{}},
      fn {json_key, atom_key, valid?, err}, {:ok, acc} ->
        case Map.fetch(cfg, json_key) do
          :error -> {:cont, {:ok, acc}}
          {:ok, v} ->
            if valid?.(v),
              do: {:cont, {:ok, Map.put(acc, atom_key, v)}},
              else: {:halt, {:error, err}}
        end
      end
    )
  end

  # Lineage: absent => no lineage; object => {parent_agent_uuid, spawn_reason}.
  # The runner does the authoritative UUID-shape check and refuses the spawn on a
  # bad parent (mapped to 422 by spawn_error/2); we only translate keys + types.
  defp fetch_lineage(body) do
    case Map.get(body, "lineage") do
      nil ->
        {:ok, nil}

      %{} = cfg ->
        with {:ok, parent} <- require_string(cfg, "parent_agent_uuid"),
             {:ok, reason} <- optional_string(cfg, "spawn_reason") do
          base = %{parent_agent_uuid: parent}
          {:ok, if(reason, do: Map.put(base, :spawn_reason, reason), else: base)}
        end

      _ ->
        {:error, "lineage must be a JSON object"}
    end
  end

  defp require_string(cfg, key) do
    case Map.get(cfg, key) do
      v when is_binary(v) and byte_size(v) > 0 -> {:ok, v}
      _ -> {:error, "#{key} is required and must be a non-empty string"}
    end
  end

  defp optional_string(cfg, key) do
    case Map.get(cfg, key) do
      nil -> {:ok, nil}
      v when is_binary(v) and byte_size(v) > 0 -> {:ok, v}
      _ -> {:error, "#{key} must be a non-empty string"}
    end
  end

  defp nonempty_string?(v), do: is_binary(v) and byte_size(v) > 0
  defp positive_integer?(v), do: is_integer(v) and v > 0

  defp put_opt(spec, _key, nil), do: spec
  defp put_opt(spec, key, value), do: Map.put(spec, key, value)

  defp put_lease(spec, :absent), do: spec
  defp put_lease(spec, value), do: Map.put(spec, :lease, value)

  # ---------- spawn-error mapping ----------

  defp spawn_error(conn, {:invalid_lineage, reason}),
    do: json(conn, 422, %{ok: false, error: "schema_invalid", detail: "invalid lineage: #{inspect(reason)}"})

  defp spawn_error(conn, {:invalid_server_url, reason}),
    do: json(conn, 422, %{ok: false, error: "schema_invalid", detail: "invalid server_url: #{inspect(reason)}"})

  defp spawn_error(conn, {:invalid_client_session_id, reason}),
    do: json(conn, 422, %{ok: false, error: "schema_invalid", detail: "invalid client_session_id: #{inspect(reason)}"})

  defp spawn_error(conn, {:executable_not_found, cmd}),
    do: json(conn, 422, %{ok: false, error: "schema_invalid", detail: "executable not found: #{cmd}"})

  defp spawn_error(conn, {:lease_denied, reason}),
    do: json(conn, 409, %{ok: false, error: "lease_denied", reason: inspect(reason)})

  defp spawn_error(conn, {:already_running, id}),
    do: json(conn, 409, %{ok: false, error: "already_running", agent_id: id})

  defp spawn_error(conn, reason) do
    Logger.error("agent orchestrator spawn failed: #{inspect(reason)}")
    json(conn, 503, %{ok: false, error: "service_unavailable", reason: "spawn failed"})
  end

  # ---------- helpers ----------

  # Default 30s, clamped to (0, 120_000]. The await holds a Bandit connection
  # open, so an unbounded client-supplied timeout would let a caller pin a
  # connection indefinitely.
  defp await_timeout(body) when is_map(body) do
    case Map.get(body, "timeout_ms") do
      n when is_integer(n) and n > 0 -> min(n, 120_000)
      _ -> 30_000
    end
  end

  defp await_timeout(_), do: 30_000

  defp check_allowed(cmd) do
    case Application.get_env(:agent_orchestrator, :cmd_allowlist) do
      nil -> :ok
      list when is_list(list) ->
        if Path.basename(cmd) in list, do: :ok, else: {:error, :cmd_not_allowed, cmd}
    end
  end

  # JSON-safe projection of the runner result. exit_status is an integer for a
  # clean exit but a `{:port_closed, reason}` tuple on an abnormal close — Jason
  # cannot encode a tuple, so stringify the non-integer case rather than crash
  # the response (which Plug.ErrorHandler would otherwise turn into a 503).
  defp present(result) do
    %{
      agent_id: result.agent_id,
      os_pid: result.os_pid,
      lease_id: result.lease_id,
      presence: result.presence,
      lineage: result.lineage,
      exit_status: encode_status(result.exit_status),
      running: result.running,
      lease_released: result.lease_released,
      output: result.output
    }
  end

  defp encode_status(nil), do: nil
  defp encode_status(n) when is_integer(n), do: n
  defp encode_status(other), do: inspect(other)

  @doc "Control-surface protocol version, surfaced in every response body."
  @spec protocol_version() :: String.t()
  def protocol_version, do: @protocol_version

  defp json(conn, status, body) do
    versioned = Map.put(body, :protocol_version, @protocol_version)

    conn
    |> Plug.Conn.put_resp_content_type("application/json")
    |> Plug.Conn.send_resp(status, Jason.encode!(versioned))
  end
end
