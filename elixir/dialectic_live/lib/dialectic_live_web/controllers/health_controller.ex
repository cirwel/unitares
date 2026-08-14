defmodule DialecticLiveWeb.HealthController do
  @moduledoc """
  Liveness endpoint for the fleet deploy tooling.

  Every other HTTP service in the fleet answers on /health, and
  `scripts/ops/deploy-status.sh` probes that path unconditionally. This app had
  no such route, so its status row read "Not Found" — indistinguishable at a
  glance from a broken service, on a dashboard whose job is telling you which
  services are broken.

  Deliberately trivial: it proves the endpoint accepted a connection and the
  router dispatched. It makes no claim about the governance firehose or the
  LiveView socket, because a health check that can fail for reasons unrelated
  to the process being up teaches operators to ignore it.
  """
  use DialecticLiveWeb, :controller

  def index(conn, _params) do
    json(conn, %{status: "ok", service: "dialectic_live"})
  end
end
