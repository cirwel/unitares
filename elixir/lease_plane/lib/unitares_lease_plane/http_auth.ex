defmodule UnitaresLeasePlane.HTTPAuth do
  @moduledoc """
  Bearer-token Plug, path-aware per RFC §7.10. Reads the expected token at
  request time from one of two config keys depending on the route:

    * `/v1/lease/force-release/*`  → `:force_release_token`
      (sourced from `LEASE_FORCE_RELEASE_TOKEN` env var)
    * everything else              → `:bearer_token`
      (sourced from `LEASE_PLANE_BEARER_TOKEN` env var)

  Both tokens come from `~/.config/cirwel/secrets.env`. Per RFC §7.10 line 731
  ("standard bearer must NOT permit force-release"), the regular bearer cannot
  authorize force-release, and the elevated force-release bearer cannot
  authorize anything else. Mutual exclusion lives at this auth layer (the
  contract layer), not at the application layer.

  Comparison is constant-time via `Plug.Crypto.secure_compare/2` so timing
  side-channels can't probe the token.

  If the relevant expected token is not configured, the plug returns 503 —
  fails closed, not silently open.
  """

  import Plug.Conn

  @behaviour Plug

  @impl true
  def init(opts), do: opts

  @impl true
  def call(conn, _opts) do
    {token_key, audience} = required_token_for(conn.path_info)
    expected = Application.get_env(:lease_plane, token_key)

    cond do
      is_nil(expected) or expected == "" ->
        send_json(conn, 503, %{
          ok: false,
          error: "service_unavailable",
          reason: "#{audience} token not configured"
        })

      authorized?(conn, expected) ->
        conn

      true ->
        send_json(conn, 401, %{
          ok: false,
          error: "permission_denied",
          reason: "#{audience} token missing or invalid"
        })
    end
  end

  # Per-path token mapping. Keep the /v1/lease/force-release/* match strict —
  # anything that doesn't match falls back to the regular bearer, so a typo
  # in the route never accidentally elevates.
  defp required_token_for(["v1", "lease", "force-release" | _]),
    do: {:force_release_token, "force-release"}

  defp required_token_for(_), do: {:bearer_token, "bearer"}

  defp authorized?(conn, expected) do
    case get_req_header(conn, "authorization") do
      [value] -> bearer_matches?(value, expected)
      _ -> false
    end
  end

  # RFC 7235 §2.1: auth scheme tokens are case-insensitive ("Bearer", "bearer",
  # "BEARER" are all valid). Several mainstream clients (Python httpx defaults,
  # some Erlang libs) send lowercase. Match the scheme case-insensitively while
  # comparing the token value with secure_compare.
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
    # Wave 2 §"Lease-integration boundary hardening" — Phase A.5 follow-on
    # to #412. The router's `json/3` helper injects `protocol_version` on
    # every response; this auth-layer helper has to do the same so a Python
    # client probing /v1/health (or any auth-gated endpoint) doesn't see an
    # envelope-shape mismatch on 401/503 vs 200. Pre-Phase-A.5 the auth 401
    # body was missing `protocol_version` and the Phase A test pinned that
    # gap (`refute Map.has_key?(decoded, "protocol_version")` on the 401
    # path). Phase A.5 closes the gap; that test inverts.
    versioned = Map.put(body, :protocol_version, UnitaresLeasePlane.HTTPRouter.protocol_version())

    conn
    |> put_resp_content_type("application/json")
    |> send_resp(status, Jason.encode!(versioned))
    |> halt()
  end
end
