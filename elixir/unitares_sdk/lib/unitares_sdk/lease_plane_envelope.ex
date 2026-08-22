defmodule UnitaresSdk.LeasePlaneEnvelope do
  @moduledoc """
  Classifier for the surface lease plane's (`:8788`) RFC §5 response envelope.

  Two repos decode this envelope by hand today — the agent orchestrator's
  `LeasePlaneClient` and dispatch_beam's `Dispatch.Lease` — and they currently
  agree. This module is where that agreement becomes a contract instead of a
  coincidence: the governance envelope went four hand-rolled decoders deep
  before their disagreement surfaced as a multi-day outage, and only then was
  it consolidated. The lease envelope gets the consolidation at two.

  Shapes collected from the live callers:

    * acquire `200` — `%{"ok" => true, "lease" => %{"lease_id" => id}}`
    * acquire `409` — `%{"error" => "held_by_other", "held_by_uuid" => ...}`
      plus fields some callers act on (dispatch_beam's reclaim reads the
      blocking lease's `intent`), so the full payload rides in the tuple
    * release `200` `%{"ok" => true}`, and `404` — both terminal-success:
      releasing an already-gone lease is not a failure
    * anything else error-keyed — a typed plane error

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
  callers with reclaim logic need more than the holder uuid, and callers that
  don't can ignore the third element.
  """
  @spec classify_acquire(non_neg_integer(), term()) :: acquire_result()
  def classify_acquire(200, %{"ok" => true, "lease" => %{"lease_id" => id}})
      when is_binary(id),
      do: {:ok, id}

  def classify_acquire(409, %{"error" => "held_by_other"} = payload),
    do: {:error, {:held_by_other, Map.get(payload, "held_by_uuid"), payload}}

  def classify_acquire(status, %{"error" => err} = payload),
    do:
      {:error,
       {:lease_plane_error, status, err, Map.get(payload, "reason") || Map.get(payload, "detail")}}

  def classify_acquire(status, payload),
    do: {:error, {:lease_plane_unexpected, status, payload}}

  @doc """
  Classify a release reply (`POST /v1/lease/release`). `404` is success — the
  lease is gone either way, and treating it as failure retries a no-op.
  """
  @spec classify_release(non_neg_integer(), term()) :: :ok | {:error, term()}
  def classify_release(200, %{"ok" => true}), do: :ok
  def classify_release(404, _payload), do: :ok

  def classify_release(status, payload),
    do: {:error, {:release_failed, status, payload}}
end
