defmodule UnitaresSdk.Identity do
  @moduledoc """
  Onboard arguments and the on-disk lineage anchor.

  The governance identity ontology (see the repo's `Strict Identity, Simple
  Contract`) in the three rules a BEAM client actually needs:

    1. Mint fresh with `force_new: true`. Never try to *resume* an identity by
       guessing — co-location is not lineage.
    2. Declare `parent_agent_id` **only** for a real handoff from a process
       that has exited. A long-lived BEAM app restarting is exactly that case:
       the previous OS process is gone and this one inherits its work, so
       chaining the prior uuid across restarts reads as one continuous lineage
       rather than a fresh anonymous agent every boot.
    3. Echo `client_session_id` on every write. A transport-injected CSID does
       not satisfy the strict gate; the value the server handed back does.

  The anchor file is the operator lever. It is a plain JSON object so it can be
  inspected and deleted by hand — deleting it makes the next boot mint a
  rootless identity rather than chaining, which is the documented way to break
  a lineage deliberately.
  """

  @type anchor :: %{optional(String.t()) => term()}

  @doc """
  Build `onboard` arguments.

  `parent` is the prior uuid from the anchor, or `nil` on a first-ever boot.
  `spawn_reason` should name the harness (`"dispatch_beam_harness"`), not the
  work — it is how fleet aggregation tells harness identities apart.
  """
  @spec onboard_args(String.t() | nil, String.t()) :: map()
  def onboard_args(parent, spawn_reason) when is_binary(spawn_reason) do
    base = %{"force_new" => true, "spawn_reason" => spawn_reason}

    if is_binary(parent) and parent != "",
      do: Map.put(base, "parent_agent_id", parent),
      else: base
  end

  @doc """
  Extract the identity fields from an `onboard` result.

  Returns `:error` when the response carries no uuid — a response that decoded
  cleanly but named no agent is not an identity, and treating it as one is how
  a client ends up writing under a phantom.
  """
  @spec from_onboard(map()) :: {:ok, map()} | :error
  def from_onboard(%{"uuid" => uuid} = r) when is_binary(uuid) and uuid != "" do
    {:ok,
     %{
       agent_id: uuid,
       client_session_id: Map.get(r, "client_session_id"),
       continuity_token: Map.get(r, "continuity_token")
     }}
  end

  def from_onboard(_), do: :error

  @doc """
  Build check-in arguments, **structurally unable to carry `agent_id`**.

  This is the one rule a BEAM governance client is most likely to get wrong,
  and getting it wrong is silent.

  A check-in must present identity by **echoing the `client_session_id` the
  server handed back**, never by declaring `agent_id`. Declaring the uuid makes
  the REST strict gate skip its typed refusal entirely: the check-in resolves
  by uuid passthrough, with no PG session renewal and no Redis re-cache. The
  binding can then be gone and every check-in still looks fine — the refusal
  that would have told you never fires.

  Found live 2026-07-03 while building `anima_broker`: the Redis-wipe
  acceptance test *passed* without ever exercising the recovery path, purely
  because `agent_id` was being sent. Dropping the key is what made the gate
  falsifiable and the test meaningful.

  `identity` is the map from `from_onboard/1`. Any `"agent_id"` in `fields` is
  dropped rather than merged — a caller cannot opt back into the bug, and the
  drop is visible in tests instead of being a comment someone deletes later.

  ## Examples

      iex> UnitaresSdk.Identity.check_in_args(%{"response_text" => "x"},
      ...>   %{agent_id: "u", client_session_id: "c", continuity_token: nil})
      %{"response_text" => "x", "client_session_id" => "c"}
  """
  @spec check_in_args(map(), map()) :: map()
  def check_in_args(fields, identity) when is_map(fields) and is_map(identity) do
    fields
    |> Map.drop(["agent_id", :agent_id])
    |> put_present("client_session_id", Map.get(identity, :client_session_id))
    |> put_present("continuity_token", Map.get(identity, :continuity_token))
  end

  @doc """
  Whether a check-in argument map violates the no-`agent_id` rule.

  For clients that build their own arguments and want the invariant asserted in
  their own suite rather than adopting `check_in_args/2` wholesale.
  """
  @spec declares_agent_id?(map()) :: boolean()
  def declares_agent_id?(args) when is_map(args),
    do: Map.has_key?(args, "agent_id") or Map.has_key?(args, :agent_id)

  defp put_present(map, _key, nil), do: map
  defp put_present(map, _key, ""), do: map
  defp put_present(map, key, value), do: Map.put(map, key, value)

  @doc """
  Read the prior uuid from the anchor file. Any failure — missing file, bad
  JSON, wrong shape — yields `nil`, which means "mint rootless". A corrupt
  anchor must never crash a boot.
  """
  @spec load_prior_uuid(Path.t() | nil) :: String.t() | nil
  def load_prior_uuid(nil), do: nil

  def load_prior_uuid(path) do
    case load_anchor(path) do
      %{"agent_id" => id} when is_binary(id) and id != "" -> id
      %{"agent_uuid" => id} when is_binary(id) and id != "" -> id
      _ -> nil
    end
  end

  @doc "Read the whole anchor object, or `nil`."
  @spec load_anchor(Path.t() | nil) :: anchor() | nil
  def load_anchor(nil), do: nil

  def load_anchor(path) do
    with {:ok, bin} <- File.read(path),
         {:ok, %{} = m} <- json_decode(bin) do
      m
    else
      _ -> nil
    end
  end

  @doc """
  Persist the anchor. Best-effort by design: a read-only or full disk must
  degrade to "next boot mints rootless", never to a crash on the identity path.
  """
  @spec persist_anchor(Path.t() | nil, anchor()) :: :ok
  def persist_anchor(nil, _anchor), do: :ok

  def persist_anchor(path, %{} = anchor) do
    File.mkdir_p(Path.dirname(path))
    File.write(path, IO.iodata_to_binary(:json.encode(anchor)))
    :ok
  rescue
    _ -> :ok
  catch
    _, _ -> :ok
  end

  defp json_decode(bin) do
    {:ok, :json.decode(bin)}
  rescue
    _ -> :error
  catch
    _, _ -> :error
  end
end
