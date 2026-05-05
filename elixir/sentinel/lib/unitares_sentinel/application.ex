defmodule UnitaresSentinel.Application do
  @moduledoc """
  OTP entry point for the Sentinel app.

  Skeleton-only at this stage: the supervisor starts with no children
  in :test mode (`config/test.exs`) and a placeholder set in :prod / :dev.
  Cycle worker, lease consumer, WebSocket ingester, and Finch HTTP client
  pool will be added by follow-up PRs as their respective surfaces (1–5)
  are wired per the v0.1.1 RFC.

  Mirrors the `:start_application` gate that `UnitaresLeasePlane.Application`
  uses (`elixir/lease_plane/lib/unitares_lease_plane/application.ex`) so
  ExUnit can boot the OTP app without a full child tree.
  """

  use Application

  @impl true
  def start(_type, _args) do
    if Application.get_env(:unitares_sentinel, :start_application, true) do
      start_full()
    else
      Supervisor.start_link([], strategy: :one_for_one, name: UnitaresSentinel.Supervisor)
    end
  end

  defp start_full do
    children = []
    Supervisor.start_link(children, strategy: :one_for_one, name: UnitaresSentinel.Supervisor)
  end
end
