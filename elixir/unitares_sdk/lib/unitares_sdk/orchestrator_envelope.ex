defmodule UnitaresSdk.OrchestratorEnvelope do
  @moduledoc """
  Classifier for the agent orchestrator's (`:8789`) response envelope.

  Exists because the envelope's one non-obvious property keeps misleading
  consumers: on `await`/`snapshot` replies **everything lives under
  `"result"`** — `running`, `output`, `exit_status` are NOT top-level keys.
  Reading `payload["running"]` at the top level yields `nil`, which reads as
  "not running" and was misread as agent-finished by two independent pollers
  on 2026-08-21 alone. `classify_result/2` returns the nested map or a typed
  error; there is no shape a caller can silently misread as completion.

  Shapes collected from the live consumers (dispatch_beam's
  `Dispatch.OrchestratorClient` and the lease plane's `agent_spawn` execute
  path):

    * spawn `201` — `%{"ok" => true, "agent_id" => id}`
    * await/snapshot `200` — `%{"ok" => true, "result" => map}` (nested)
    * stop `200`/`404` — both mean the agent is gone
    * anything error-keyed — a typed orchestrator error, never `{:ok, _}`

  Pure classifier over ALREADY-DECODED terms. Transport and JSON decoding
  stay with each caller — the `UnitaresSdk.Envelope` lesson holds here too:
  what clients get wrong is reading the reply, never making the call.
  """

  @type spawn_result :: {:ok, String.t()} | {:error, term()}
  @type result_result :: {:ok, map()} | {:error, term()}

  @doc """
  Classify a spawn reply (`POST /v1/agents`). `{:ok, agent_id}` only on a
  `201` carrying `"ok" => true` and a binary `"agent_id"`.
  """
  @spec classify_spawn(non_neg_integer(), term()) :: spawn_result()
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
  `"agent_id"`. A `200` body without a map under `"result"` is a typed error,
  never a success: that is the flat-read trap this module exists to close.
  """
  @spec classify_result(non_neg_integer(), term()) :: result_result()
  def classify_result(200, %{"ok" => true, "result" => result}) when is_map(result),
    do: {:ok, result}

  def classify_result(404, _payload), do: {:error, :not_found}

  def classify_result(status, %{"error" => err} = payload),
    do: {:error, {:orchestrator_error, status, err, payload}}

  def classify_result(status, payload),
    do: {:error, {:orchestrator_unexpected, status, payload}}

  @doc """
  Classify a stop/delete reply. `200` and `404` both mean the agent is gone —
  deleting an already-exited agent is success, not failure.
  """
  @spec classify_stop(non_neg_integer(), term()) :: :ok | {:error, term()}
  def classify_stop(status, _payload) when status in [200, 404], do: :ok

  def classify_stop(status, payload),
    do: {:error, {:orchestrator_error, status, nil, payload}}
end
