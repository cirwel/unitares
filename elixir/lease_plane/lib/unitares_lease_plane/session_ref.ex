defmodule UnitaresLeasePlane.SessionRef do
  @moduledoc """
  The form in which a lease stores its `audit_session`.

  Governance issues keyed client session ids, `agent-{uuid12}-{tag}`, that
  authenticate as their agent, and `/v1/lease/status` returns `audit_session`
  to any holder of the lease bearer token. A keyed id is therefore stored as
  the same `csid:` digest governance writes to its own audit rows
  (`src/mcp_handlers/identity/stable_session.py`, `audit_reference`), which
  still tells one session's leases apart. Any other value, including a digest
  a client already computed, is stored as given.
  """

  @keyed ~r/\Aagent-[0-9a-f]{8}-[0-9a-f]{3}-[a-z2-7]{20}\z/

  @spec reference(term()) :: term()
  def reference(session) when is_binary(session) do
    if Regex.match?(@keyed, session) do
      digest = :crypto.hash(:sha256, session) |> Base.encode16(case: :lower)
      "csid:" <> binary_part(digest, 0, 24)
    else
      session
    end
  end

  def reference(other), do: other
end
