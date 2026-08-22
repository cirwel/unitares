defmodule AgentOrchestrator.LeasePlaneClient do
  @moduledoc """
  Thin HTTP client for the lease plane's RFC §5 surface, used by `AgentRunner`
  to bind an ephemeral agent to a lease.

  Uses OTP's built-in `:httpc` so the orchestrator carries no third-party HTTP
  dependency. The plane is localhost-only (IPv4 127.0.0.1:8788) and bearer-auth'd;
  this client fails closed when the bearer is absent rather than calling unauth'd.

  The three functions form a behaviour so tests (and standalone runs without a
  live plane) can inject a stub via the `:lease_client` spec key.
  """

  @behaviour AgentOrchestrator.LeasePlaneClient.Behaviour

  require Logger

  @impl true
  def acquire(surface_id, holder_agent_uuid, holder_kind, ttl_s) do
    body = %{
      surface_id: surface_id,
      holder_agent_uuid: holder_agent_uuid,
      holder_kind: holder_kind,
      ttl_s: ttl_s
    }

    # Reply-reading is SDK-owned (UnitaresSdk.LeasePlaneEnvelope): one
    # authoritative decode, eligible to stop drifting from dispatch_beam's
    # once that repo's pinned SDK ref moves past this commit. Transport stays
    # :httpc here. SDK error terms pass through UNCHANGED — held_by_other is
    # a 3-tuple whose payload carries retry_after_hint_ms/blocking_lease_id;
    # AgentRunner treats reasons opaquely, so no re-narrowing adapter.
    case post("/v1/lease/acquire", body) do
      {:ok, status, payload} ->
        UnitaresSdk.LeasePlaneEnvelope.classify_acquire(status, payload)

      {:error, reason} ->
        {:error, reason}
    end
  end

  @impl true
  def release(lease_id, reason) do
    # `release_reason` is a CLOSED server-side enum (http_router.ex release
    # handler): normal | down_local | reaped_after_supervisor_failed |
    # reaped_local_ttl | reaped_remote_ttl | handoff | reclaimed_lost_acquire
    # ("forced" is rejected here — use /v1/lease/force-release). An ad-hoc
    # reason gets 422 schema_invalid, classifies as {:release_refused, ...},
    # and the lease STAYS HELD (live-verified 2026-08-22). Deliberately not
    # allowlisted client-side: a stale copy of the enum on this fail path
    # would refuse valid reasons after a server addition.
    case post("/v1/lease/release", %{lease_id: lease_id, release_reason: reason}) do
      {:ok, status, payload} ->
        UnitaresSdk.LeasePlaneEnvelope.classify_release(status, payload)

      {:error, reason} ->
        {:error, reason}
    end
  end

  # ---------- internals ----------

  defp post(path, body) do
    with {:ok, token} <- bearer() do
      url = base_url() <> path
      headers = [{~c"authorization", String.to_charlist("Bearer " <> token)}]
      payload = Jason.encode!(body)

      request = {String.to_charlist(url), headers, ~c"application/json", payload}

      case :httpc.request(:post, request, [{:timeout, 5_000}, {:connect_timeout, 2_000}], []) do
        {:ok, {{_http, status, _reason}, _resp_headers, resp_body}} ->
          {:ok, status, decode(resp_body)}

        {:error, reason} ->
          {:error, {:http, reason}}
      end
    end
  end

  defp bearer do
    case Application.get_env(:agent_orchestrator, :lease_plane_bearer_token) do
      nil -> {:error, :no_bearer}
      "" -> {:error, :no_bearer}
      token -> {:ok, token}
    end
  end

  defp base_url do
    Application.get_env(:agent_orchestrator, :lease_plane_base_url, "http://127.0.0.1:8788")
  end

  defp decode(body) when is_list(body), do: decode(IO.iodata_to_binary(body))

  defp decode(body) when is_binary(body) do
    case Jason.decode(body) do
      {:ok, map} -> map
      {:error, _} -> %{"raw" => body}
    end
  end
end
