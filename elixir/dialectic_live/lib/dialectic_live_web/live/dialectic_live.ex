defmodule DialecticLiveWeb.DialecticLive do
  @moduledoc """
  The beachhead pane: live-ish list of dialectic sessions.

  B1 contract (what works against the current server):
    * mount loads sessions via `DialecticLive.Governance.list_sessions/0`
    * subscribes to the `"dialectic:events"` PubSub topic; any event is a doorbell
      to refetch (mirrors the JS dashboard's ws.js). A 10s timer is the floor so
      the pane stays fresh even before dialectic_* events exist server-side.
    * sessions with `awaiting_facilitation == true` float to the top and get a
      badge — these are the #1015 human-facilitation-needed sessions. (Depends on
      the server exposing the field in `list`; PR #1220 / migration 053. Until
      then the field is simply absent and nothing is badged — no crash.)

  B2 (NOT here): true per-turn streaming needs the engine to emit dialectic_*
  events (#1167 Ask 1). The topic + doorbell are ready for that upgrade.
  """
  use DialecticLiveWeb, :live_view

  alias DialecticLive.{Governance, Firehose}

  @refresh_ms 10_000

  @impl true
  def mount(_params, _session, socket) do
    if connected?(socket) do
      Phoenix.PubSub.subscribe(DialecticLive.PubSub, Firehose.dialectic_topic())
      :timer.send_interval(@refresh_ms, self(), :refresh)
    end

    {:ok, socket |> assign(error: nil) |> load_sessions()}
  end

  @impl true
  def handle_info(:refresh, socket), do: {:noreply, load_sessions(socket)}

  # Doorbell: a dialectic_* event arrived → refetch authoritative state.
  def handle_info({:governance_event, _event}, socket), do: {:noreply, load_sessions(socket)}

  @impl true
  def handle_event("refresh", _params, socket), do: {:noreply, load_sessions(socket)}

  defp load_sessions(socket) do
    case Governance.list_sessions() do
      {:ok, sessions} ->
        assign(socket, sessions: sort_sessions(sessions), error: nil, last_ok: true)

      {:error, reason} ->
        socket
        |> assign(error: "governance unavailable: #{inspect(reason)}")
        |> assign_new(:sessions, fn -> [] end)
    end
  end

  # awaiting-facilitation first, then most-recently-updated.
  defp sort_sessions(sessions) do
    Enum.sort_by(sessions, fn s ->
      {awaiting?(s) && 0 || 1, -updated_sort_key(s)}
    end)
  end

  defp awaiting?(s), do: truthy(field(s, "awaiting_facilitation"))

  defp updated_sort_key(s) do
    case field(s, "updated_at") || field(s, "created_at") do
      v when is_integer(v) -> v
      v when is_binary(v) -> String.length(v) # stable-ish; refine when shape settles
      _ -> 0
    end
  end

  defp field(s, key) when is_map(s), do: Map.get(s, key) || Map.get(s, String.to_atom(key))
  defp field(_, _), do: nil

  defp truthy(true), do: true
  defp truthy("true"), do: true
  defp truthy(1), do: true
  defp truthy(_), do: false

  @impl true
  def render(assigns) do
    ~H"""
    <div class="mx-auto max-w-4xl p-6">
      <div class="flex items-center justify-between mb-6">
        <h1 class="text-2xl font-semibold">Dialectic — live sessions</h1>
        <button phx-click="refresh" class="btn btn-sm btn-outline">Refresh</button>
      </div>

      <div :if={@error} class="alert alert-warning mb-4">
        <span>{@error}</span>
      </div>

      <div :if={@sessions == []} class="text-base-content/60">
        No active dialectic sessions.
      </div>

      <ul class="space-y-3">
        <li :for={s <- @sessions} class={["card bg-base-200 p-4", awaiting?(s) && "border border-warning"]}>
          <div class="flex items-center justify-between gap-3">
            <div class="font-mono text-sm truncate">{field(s, "session_id") || field(s, "id") || "—"}</div>
            <div class="flex items-center gap-2">
              <span :if={awaiting?(s)} class="badge badge-warning">awaiting facilitation</span>
              <span class="badge badge-ghost">{field(s, "phase") || field(s, "status") || "?"}</span>
            </div>
          </div>
          <div :if={field(s, "topic") || field(s, "question")} class="text-sm text-base-content/70 mt-1">
            {field(s, "topic") || field(s, "question")}
          </div>
        </li>
      </ul>
    </div>
    """
  end
end
