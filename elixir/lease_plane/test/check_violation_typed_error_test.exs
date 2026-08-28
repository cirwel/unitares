defmodule UnitaresLeasePlane.CheckViolationTypedErrorTest do
  @moduledoc """
  RFC §7.13.5: a DB CHECK violation is a caller error (422 schema_invalid),
  never a transient outage (503 service_unavailable).

  ⛔These tests exist because the contract was documented but not served. Both
  router arms matched `constraint_name:` on `Postgrex.Error.postgres`, whose
  `@metadata` is `[:table, :column, :constraint, :hint]` — so the key was never
  present, the clause could never match, and every CHECK violation fell through
  to the generic 503 arm. The comment above each clause said "MUST precede the
  generic arm — falling through to 503 would mask a writer bug as a transient
  outage", which is exactly what was happening.

  Both tests fail on the unfixed router (503 instead of 422).
  """

  use ExUnit.Case, async: false

  import Plug.Test
  import Plug.Conn
  import LeaseTestHelpers

  alias UnitaresLeasePlane.HTTPRouter

  @opts HTTPRouter.init([])
  @bearer "test-bearer-token-do-not-use-in-prod"

  setup do
    Application.put_env(:lease_plane, :bearer_token, @bearer)
    Application.put_env(:lease_plane, :identity_binding_mode, :off)
    surface = unique_surface_id("checkviol")

    on_exit(fn -> cleanup_surface(surface) end)

    {:ok, surface: surface}
  end

  defp post_json(path, body) do
    :post
    |> conn(path, Jason.encode!(body))
    |> put_req_header("content-type", "application/json")
    |> put_req_header("authorization", "Bearer #{@bearer}")
    |> HTTPRouter.call(@opts)
  end

  defp parsed(conn), do: Jason.decode!(conn.resp_body)

  # substrate_state_only_on_resident_kind: substrate_state may be non-null only
  # when surface_kind = 'resident'. A file:// surface therefore violates it.
  defp acquire_body(surface) do
    %{
      surface_id: surface,
      holder_agent_uuid: random_uuid(),
      holder_kind: "remote_heartbeat",
      holder_class: "process_instance",
      ttl_s: 60,
      substrate_state: %{"sensor" => %{"status" => "healthy"}},
      substrate_state_observed_at: DateTime.utc_now() |> DateTime.to_iso8601()
    }
  end

  test "acquire maps a CHECK violation to 422, not 503", ctx do
    resp = post_json("/v1/lease/acquire", acquire_body(ctx.surface))

    assert resp.status == 422,
           "CHECK violation must be a typed caller error, got #{resp.status}"

    body = parsed(resp)
    assert body["error"] == "schema_invalid"

    # The constraint name is the machine-readable discriminator the RFC
    # promises; without it a caller cannot tell WHICH check it tripped.
    assert body["detail"] == "substrate_state_only_on_resident_kind"
  end

  test "renew maps a CHECK violation to 422, not 503", ctx do
    # A clean acquire first, so renew has a live lease to operate on.
    clean =
      ctx.surface
      |> acquire_body()
      |> Map.drop([:substrate_state, :substrate_state_observed_at])

    acquired = post_json("/v1/lease/acquire", clean)
    assert acquired.status == 200
    lease_id = parsed(acquired)["lease"]["lease_id"]

    resp =
      post_json("/v1/lease/renew", %{
        lease_id: lease_id,
        substrate_state: %{"sensor" => %{"status" => "healthy"}},
        substrate_state_observed_at: DateTime.utc_now() |> DateTime.to_iso8601()
      })

    assert resp.status == 422,
           "CHECK violation on renew must be a typed caller error, got #{resp.status}"

    assert parsed(resp)["error"] == "schema_invalid"
    assert parsed(resp)["detail"] == "substrate_state_only_on_resident_kind"
  end
end
