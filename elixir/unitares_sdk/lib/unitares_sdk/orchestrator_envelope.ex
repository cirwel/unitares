defmodule UnitaresSdk.OrchestratorEnvelope do
  @moduledoc """
  Classifier for the agent orchestrator's (`:8789`) response envelope.

  Exists because the envelope's one non-obvious property misleads consumers:
  on `await`/`snapshot` replies **everything lives under `"result"`** —
  `running`, `output`, `exit_status` are NOT top-level keys. Reading
  `payload["running"]` at the top level yields `nil`, which reads as "not
  running": a session-side poller made exactly that misread on 2026-08-21
  and declared a running agent finished after zero seconds. `classify_result/2`
  returns the nested map or a typed error; there is no shape a caller can
  silently misread as completion.

  Shapes verified against the running orchestrator and the in-tree router
  (`elixir/agent_orchestrator/lib/agent_orchestrator/http_router.ex`),
  including the branches the live consumers depend on:

    * spawn `201` v0.2 — `%{"ok" => true, "execution_id" => id, "agent_id" => id}`
      (`agent_id`-only v0.1 replies remain accepted)
    * await/snapshot `200` — `%{"ok" => true, "result" => map}` (nested)
    * await `504` — `%{"ok" => false, "error" => "await_timeout"}`, distinct
      from `not_found` BY DESIGN so callers re-await or snapshot instead of
      reaping. dispatch_beam's council loop depends on this distinction —
      it re-awaits on timeout and reaps on every other error.
    * stop `200 %{"ok" => true}` / `404` — both mean the agent is not
      stoppable. CAVEATS: a DELETE `404` does not imply the record is gone —
      a subsequent GET on the same id can still return a full result
      (live-verified 2026-08-22); and the router answers unknown ROUTES with
      the same `404`, so a misrouted DELETE also reads as "gone". Callers
      with reap-critical semantics should confirm via `classify_result/2`.

  Pure classifier over ALREADY-DECODED terms. Transport and JSON decoding
  stay with each caller — the `UnitaresSdk.Envelope` lesson holds here too:
  what clients get wrong is reading the reply, never making the call.
  dispatch_beam adopts by moving its pinned SDK ref past this commit.
  """

  @type spawn_result :: {:ok, String.t()} | {:error, term()}
  @type result_result :: {:ok, map()} | {:error, term()}

  @doc """
  Classify a spawn reply (`POST /v1/agents`). Returns the immutable execution
  id. v0.2's explicit `execution_id` wins; the `agent_id` fallback keeps clients
  compatible with a v0.1 orchestrator during rolling deploys.
  """
  @spec classify_spawn(non_neg_integer(), term()) :: spawn_result()
  def classify_spawn(201, %{"ok" => true, "execution_id" => id}) when is_binary(id),
    do: {:ok, id}

  def classify_spawn(201, %{"ok" => true, "agent_id" => id}) when is_binary(id),
    do: {:ok, id}

  def classify_spawn(status, %{"error" => err} = payload),
    do: {:error, {:orchestrator_error, status, err, payload}}

  def classify_spawn(status, payload),
    do: {:error, {:orchestrator_unexpected, status, payload}}

  @doc """
  Classify an await/snapshot reply (`GET /v1/agents/:id`,
  `POST /v1/agents/:id/await`). On success returns the **nested** result map —
  the map that actually carries `"running"`, `"output"`, `"exit_status"`,
  `"execution_id"` and logical `"agent_id"`. A `200` body without a map under
  `"result"` is a typed error,
  never a success: that is the flat-read trap this module exists to close.

  `{:error, :await_timeout}` is a control signal, not a failure: the agent is
  still running and the caller should re-await or snapshot. Classifying it as
  a generic error and reaping kills healthy long-running agents.
  """
  @spec classify_result(non_neg_integer(), term()) :: result_result()
  def classify_result(200, %{"ok" => true, "result" => result}) when is_map(result),
    do: {:ok, result}

  def classify_result(504, %{"error" => "await_timeout"}), do: {:error, :await_timeout}

  def classify_result(404, _payload), do: {:error, :not_found}

  def classify_result(status, %{"error" => err} = payload),
    do: {:error, {:orchestrator_error, status, err, payload}}

  def classify_result(status, payload),
    do: {:error, {:orchestrator_unexpected, status, payload}}

  @doc """
  Classify a stop/delete reply. A `200` must carry `"ok" => true`; `404`
  means the agent already exited or was reaped (see the moduledoc caveats —
  gone-to-DELETE does not mean gone-to-GET, and an unroutable path 404s the
  same way). Error envelopes surface their `"error"` name.
  """
  @spec classify_stop(non_neg_integer(), term()) :: :ok | {:error, term()}
  def classify_stop(200, %{"ok" => true}), do: :ok
  def classify_stop(404, _payload), do: :ok

  def classify_stop(status, %{"error" => err} = payload),
    do: {:error, {:orchestrator_error, status, err, payload}}

  def classify_stop(status, payload),
    do: {:error, {:orchestrator_unexpected, status, payload}}
end
