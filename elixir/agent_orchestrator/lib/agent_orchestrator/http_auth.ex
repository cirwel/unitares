defmodule AgentOrchestrator.HTTPAuth do
  @moduledoc """
  Bearer-token Plug for the orchestrator's control surface.

  Reads the expected token at request time from `:agent_orchestrator,
  :bearer_token` (sourced from `AGENT_ORCHESTRATOR_BEARER_TOKEN` at boot — see
  `AgentOrchestrator.Application`). Comparison is constant-time via
  `Plug.Crypto.secure_compare/2` so timing side-channels cannot probe the token.

  ## Why this matters more here than on the lease plane

  This surface spawns OS processes (`POST /v1/agents` with an arbitrary `cmd`).
  An unauthenticated caller reaching it is remote code execution. The posture is
  therefore identical to, and at least as strict as, the lease plane:

    * **Fail closed.** If no token is configured the plug returns 503 — never
      silently open. A misconfigured deploy refuses every request instead of
      accepting them unauthenticated.
    * **Localhost-only bind** (enforced in `Application`, not here) — a single
      trust boundary at `127.0.0.1`.

  Auth runs BEFORE body parsing in the router so an unauthenticated caller
  cannot probe endpoint existence (or trip the spawn path) by sending a body;
  bearer auth only reads the `authorization` header.
  """

  import Plug.Conn

  @behaviour Plug

  @impl true
  def init(opts), do: opts

  @impl true
  def call(conn, _opts) do
    expected = Application.get_env(:agent_orchestrator, :bearer_token)

    cond do
      is_nil(expected) or expected == "" ->
        send_json(conn, 503, %{
          ok: false,
          error: "service_unavailable",
          reason: "bearer token not configured"
        })

      authorized?(conn, expected) ->
        conn

      true ->
        send_json(conn, 401, %{
          ok: false,
          error: "permission_denied",
          reason: "bearer token missing or invalid"
        })
    end
  end

  defp authorized?(conn, expected) do
    case get_req_header(conn, "authorization") do
      [value] -> bearer_matches?(value, expected)
      _ -> false
    end
  end

  # RFC 7235 §2.1: the auth scheme token is case-insensitive ("Bearer",
  # "bearer", "BEARER" all valid). Match the scheme case-insensitively, compare
  # the credential with secure_compare.
  defp bearer_matches?(value, expected) do
    case String.split(value, " ", parts: 2) do
      [scheme, presented] ->
        String.downcase(scheme) == "bearer" and
          Plug.Crypto.secure_compare(presented, expected)

      _ ->
        false
    end
  end

  defp send_json(conn, status, body) do
    conn
    |> put_resp_content_type("application/json")
    |> send_resp(status, Jason.encode!(body))
    |> halt()
  end
end
