defmodule UnitaresSdk.LeasePlaneEnvelope do
  @moduledoc """
  Classifier for the surface lease plane's (`:8788`) RFC §5 response envelope.

  Two repos decode this envelope by hand today — the agent orchestrator's
  `LeasePlaneClient` and dispatch_beam's `Dispatch.Lease` — and they currently
  agree. This module is where that agreement becomes a contract instead of a
  coincidence: the governance envelope went four hand-rolled decoders deep
  before their disagreement surfaced as a multi-day outage; the lease envelope
  gets the consolidation at two. dispatch_beam adopts by moving its pinned SDK
  ref past this commit — until then the contract binds only this tree.

  Shapes verified against the running plane and the in-tree router
  (`elixir/lease_plane/lib/unitares_lease_plane/http_router.ex`):

    * acquire `200` — `%{"ok" => true, "lease" => %{"lease_id" => id}}` plus
      top-level extras (`"idempotent"`, `"drift_warning"`) the subset match
      ignores
    * acquire `409` — `%{"error" => "held_by_other"}` with `held_by_uuid`,
      `blocking_lease_id`, `expires_at`, `retry_after_hint_ms`. The full
      payload rides in the tuple because callers act on those fields —
      `retry_after_hint_ms` exists because dropping 409 detail once broke
      acquire retries in production. NOTE: the 409 body does NOT carry the
      blocking lease's `intent`; dispatch_beam's reclaim reads intent via a
      separate `GET /v1/lease/status`.
    * policy refusals arrive as `200`/other with `"ok" => false` and an
      `"error"` name (RFC §7.3.5) — classified by error name, never as
      success
    * release `200` `%{"ok" => true}`, and `404` — both terminal-success:
      releasing an already-gone lease is not a failure. `release_reason` is a
      closed server-side enum; an unknown reason gets `422 schema_invalid`
      and classifies as a refusal (the lease stays held).

  Pure classifier over ALREADY-DECODED terms; transport and JSON stay with
  each caller.
  """

  @type acquire_result ::
          {:ok, String.t()}
          | {:error, {:held_by_other, String.t() | nil, map()}}
          | {:error, term()}

  @doc """
  Classify an acquire reply (`POST /v1/lease/acquire`).

  `{:error, {:held_by_other, held_by_uuid, payload}}` keeps the whole payload:
  `retry_after_hint_ms` / `blocking_lease_id` / `expires_at` are in it and
  retry-shaped callers need them.
  """
  @spec classify_acquire(non_neg_integer(), term()) :: acquire_result()
  def classify_acquire(200, %{"ok" => true, "lease" => %{"lease_id" => id}})
      when is_binary(id),
      do: {:ok, id}

  def classify_acquire(409, %{"error" => "held_by_other"} = payload),
    do: {:error, {:held_by_other, Map.get(payload, "held_by_uuid"), payload}}

  def classify_acquire(status, %{"error" => err} = payload) do
    detail = Map.get(payload, "reason") || Map.get(payload, "detail")
    {:error, {:lease_plane_error, status, err, detail}}
  end

  def classify_acquire(status, payload),
    do: {:error, {:lease_plane_unexpected, status, payload}}

  @doc """
  Classify a release reply (`POST /v1/lease/release`). `404` is success — the
  lease is gone either way, and treating it as failure retries a no-op. A
  refusal with an error name (RFC §7.3.5 policy envelope, or the 422
  `release_reason`-enum rejection) is typed with the name surfaced, never
  buried in an opaque blob.
  """
  @spec classify_release(non_neg_integer(), term()) :: :ok | {:error, term()}
  def classify_release(200, %{"ok" => true}), do: :ok
  def classify_release(404, _payload), do: :ok

  def classify_release(status, %{"error" => err} = payload),
    do: {:error, {:release_refused, status, err, payload}}

  def classify_release(status, payload),
    do: {:error, {:release_failed, status, payload}}
end
