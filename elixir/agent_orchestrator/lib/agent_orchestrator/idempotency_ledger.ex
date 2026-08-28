defmodule AgentOrchestrator.IdempotencyLedger do
  @moduledoc """
  Storage contract for keyed spawn reservations.

  A ledger implementation must atomically reserve one execution id for a key
  hash and material-spec digest. `:reserved` is written before the OS process
  starts; `mark_started/3` records the successful spawn afterwards. Replaying a
  still-reserved row is crash-ambiguous and must never start another process.
  """

  @type replay_state :: :reserved | :started
  @type reserve_result ::
          {:ok, :reserved}
          | {:ok, {:replay, String.t(), replay_state()}}
          | {:error, :idempotency_conflict | :idempotency_unavailable | term()}

  @callback reserve(String.t(), String.t(), String.t(), pos_integer()) :: reserve_result()
  @callback mark_started(String.t(), String.t(), String.t()) :: :ok | {:error, term()}
  @callback release_reservation(String.t(), String.t(), String.t()) ::
              :ok | {:error, term()}
  @callback sweep() :: :ok | {:error, term()}
  @callback status() :: map()
end
