defmodule UnitaresLeasePlane.HTTPAuth do
  @moduledoc """
  Bearer-token Plug. Reads the expected token at request time from
  `Application.get_env(:lease_plane, :bearer_token)` (set at boot from the
  `LEASE_PLANE_BEARER_TOKEN` env var, sourced from
  `~/.config/cirwel/secrets.env`).

  Comparison is constant-time via `Plug.Crypto.secure_compare/2` so timing
  side-channels can't probe the token.

  If no expected token is configured, the plug returns 503 — fails closed,
  not silently open.
  """

  import Plug.Conn

  @behaviour Plug

  @impl true
  def init(opts), do: opts

  @impl true
  def call(conn, _opts) do
    expected = Application.get_env(:lease_plane, :bearer_token)

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
      ["Bearer " <> presented] -> Plug.Crypto.secure_compare(presented, expected)
      _ -> false
    end
  end

  defp send_json(conn, status, body) do
    conn
    |> put_resp_content_type("application/json")
    |> send_resp(status, Jason.encode!(body))
    |> halt()
  end
end
