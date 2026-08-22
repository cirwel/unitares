defmodule AgentOrchestrator.LeasePlaneClient.Behaviour do
  @moduledoc """
  The lease-binding contract `AgentRunner` depends on. The default
  implementation is `AgentOrchestrator.LeasePlaneClient` (real HTTP); tests and
  standalone runs inject a stub via the `:lease_client` spec key.

  Error reasons are `UnitaresSdk.LeasePlaneEnvelope` terms passed through
  unchanged (e.g. `{:held_by_other, uuid, payload}` — 3-tuple, payload carries
  the 409's retry fields). `AgentRunner` treats reasons opaquely; anything
  matching on a specific reason shape must track the SDK's tags.
  """

  @callback acquire(
              surface_id :: String.t(),
              holder_agent_uuid :: String.t(),
              holder_kind :: String.t(),
              ttl_s :: pos_integer()
            ) :: {:ok, String.t()} | {:error, term()}

  @callback release(lease_id :: String.t(), reason :: String.t()) :: :ok | {:error, term()}
end
