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
