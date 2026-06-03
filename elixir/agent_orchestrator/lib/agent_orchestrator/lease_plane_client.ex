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

    case post("/v1/lease/acquire", body) do
      {:ok, 200, %{"ok" => true, "lease" => %{"lease_id" => lease_id}}} ->
        {:ok, lease_id}

      {:ok, 409, %{"error" => "held_by_other"} = info} ->
        {:error, {:held_by_other, Map.get(info, "held_by_uuid")}}

      {:ok, status, %{"error" => err} = info} ->
        {:error, {:lease_plane_error, status, err, Map.get(info, "reason") || Map.get(info, "detail")}}

      {:ok, status, _body} ->
        {:error, {:lease_plane_unexpected, status}}

      {:error, reason} ->
        {:error, reason}
    end
  end

  @impl true
  def release(lease_id, reason) do
    case post("/v1/lease/release", %{lease_id: lease_id, release_reason: reason}) do
      {:ok, 200, %{"ok" => true}} -> :ok
      {:ok, 404, _} -> :ok
      {:ok, status, body} -> {:error, {:release_failed, status, body}}
      {:error, reason} -> {:error, reason}
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
