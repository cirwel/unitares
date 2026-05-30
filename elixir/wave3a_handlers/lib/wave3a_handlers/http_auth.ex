defmodule Wave3aHandlers.HTTPAuth do
  @moduledoc """
  Bearer-token Plug for the Wave 3a BEAM handler app.

  Reads `:beam_token` from Application env (sourced from
  `WAVE_3A_BEAM_TOKEN`). Single token surface — RFC §2.5 keeps the BEAM
  inbound bearer (this) distinct from the probe-outbound bearer
  (`WAVE_3A_PROBE_TOKEN`, used by `Wave3aHandlers.ProbeClient`); they
  rotate independently and scope distinct trust boundaries.

  Comparison is constant-time via `Plug.Crypto.secure_compare/2`. If the
  expected token is not configured, the plug returns 503 — fails closed,
  not silently open. This is the same posture as
  `UnitaresLeasePlane.HTTPAuth`.

  ## Route exemption

  `GET /health` is exempt from bearer auth — it's the unauthenticated
  liveness probe analogous to PR #1's `/v1/probe/health`. Liveness has to
  work even when the token is rotating; gating it would mean a token
  rotation could mask a real outage. Every other route is bearer-gated.
  """

  import Plug.Conn

  @behaviour Plug

  @impl true
  def init(opts), do: opts

  @impl true
  def call(conn, _opts) do
    if auth_exempt?(conn) do
      conn
    else
      expected = Application.get_env(:wave3a_handlers, :beam_token)

      cond do
        is_nil(expected) or expected == "" ->
          send_json(conn, 503, %{
            ok: false,
            protocol_version: Wave3aHandlers.HTTPRouter.protocol_version(),
            error: "service_unavailable",
            reason: "WAVE_3A_BEAM_TOKEN not configured"
          })

        authorized?(conn, expected) ->
          conn

        true ->
          send_json(conn, 401, %{
            ok: false,
            protocol_version: Wave3aHandlers.HTTPRouter.protocol_version(),
            error: "permission_denied",
            reason: "bearer token missing or invalid"
          })
      end
    end
  end

  # Liveness probe is auth-exempt (see @moduledoc).
  defp auth_exempt?(%Plug.Conn{method: "GET", path_info: ["health"]}), do: true
  defp auth_exempt?(_), do: false

  defp authorized?(conn, expected) do
    case get_req_header(conn, "authorization") do
      [value] -> bearer_matches?(value, expected)
      _ -> false
    end
  end

  # RFC 7235 §2.1: auth scheme tokens are case-insensitive. Same logic
  # lease plane uses — accept lowercase "bearer" from clients (Python httpx
  # default) so we don't break interoperability for no security gain.
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
