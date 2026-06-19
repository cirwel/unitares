defmodule UnitaresLeasePlane.GovernedEffectIntegrationTest do
  @moduledoc """
  Observe-not-acquire path exercised end-to-end through the HTTP router against
  a real lease row (DB-backed, like http_router_test). Confirms a record_only
  shadow reports `would_block` for a held surface and `ok` for a free one —
  without ever acquiring (the surface stays held by its real holder).
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
    surface = unique_surface_id("effect")
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

  defp effect_body(surface, overrides \\ %{}) do
    Map.merge(
      %{
        idempotency_key: "idem-#{System.unique_integer([:positive])}",
        custody_mode: "record_only",
        effect_type: "file_write",
        surface: "repo://unitares/doc_update",
        required_leases: [%{surface: surface}]
      },
      overrides
    )
  end

  test "free surface → would_acquire ok", ctx do
    resp = post_json("/v1/effects", effect_body(ctx.surface))
    assert resp.status == 202
    body = parsed(resp)
    assert body["custody_mode"] == "record_only"
    assert body["status"] == "committed"
    assert [obs] = body["observations"]
    assert obs["would_acquire"] == "ok"
    assert obs["surface"] == ctx.surface
  end

  test "held surface → would_block names the holder, without acquiring", ctx do
    acq =
      post_json(
        "/v1/lease/acquire",
        Map.put(local_beam_params(ctx.surface), :holder_kind, "local_beam")
      )

    assert acq.status == 200
    holder = parsed(acq)["lease"]["holder_agent_uuid"]

    resp = post_json("/v1/effects", effect_body(ctx.surface))
    assert resp.status == 202
    [obs] = parsed(resp)["observations"]
    assert obs["would_acquire"] == "would_block"
    assert obs["held_by_uuid"] == holder

    # observe-not-acquire: a second observation still reports the SAME holder —
    # the shadow neither stole, replaced, nor released the original lease.
    resp2 = post_json("/v1/effects", effect_body(ctx.surface))
    [obs2] = parsed(resp2)["observations"]
    assert obs2["would_acquire"] == "would_block"
    assert obs2["held_by_uuid"] == holder
  end

  test "execute mode → 501 not_implemented", ctx do
    resp = post_json("/v1/effects", effect_body(ctx.surface, %{custody_mode: "execute"}))
    assert resp.status == 501
    assert parsed(resp)["error"] == "not_implemented"
  end

  test "malformed envelope → 422 schema_invalid", ctx do
    resp = post_json("/v1/effects", effect_body(ctx.surface) |> Map.delete(:idempotency_key))
    assert resp.status == 422
    assert parsed(resp)["error"] == "schema_invalid"
  end
end
