defmodule UnitaresSentinel.LeaseAdvisory do
  @moduledoc """
  Best-effort Phase A lease-plane advisory client for BEAM Sentinel.

  Mirrors `src/lease_plane/advisory.py`: missing token, network errors,
  schema errors, and held-by-other responses are telemetry only. Callers get
  a classified outcome, and lease-layer failures never block Sentinel work.
  """

  require Logger

  @default_base_url "http://127.0.0.1:8788"
  @default_timeout_ms 2_000
  @cycle_surface_id "resident:/sentinel_cycle"
  @cycle_holder_kind "remote_heartbeat"
  @cycle_ttl_s 300
  @cycle_intent "sentinel analysis cycle"

  @type outcome ::
          :acquired_new
          | :acquired_idempotent
          | :enforcement_blocked
          | :held_by_other
          | :service_unavailable
          | :permission_denied
          | :schema_invalid
          | :client_error

  @typedoc """
  Diagnostic detail carried alongside a non-acquiring outcome.

  Added 2026-07-31 (immortal-lease incident): the lease plane has always sent
  `blocking_lease_id` on the 409 `held_by_other` body
  (`elixir/lease_plane/lib/unitares_lease_plane/http_router.ex:84-92`, populated
  by `repo.ex:113-120`), but this client read only `held_by_uuid` for a log line
  and dropped the rest. `blocking_lease_id` is the exact argument to
  `POST /v1/lease/force-release`, so a starvation finding that omits it is
  informative but not actionable.
  """
  @type conflict :: %{
          optional(:surface_id) => String.t() | nil,
          optional(:blocking_lease_id) => String.t() | nil,
          optional(:held_by_uuid) => String.t() | nil,
          optional(:expires_at) => String.t() | nil,
          optional(:blocked_outcome) => outcome(),
          optional(:attempted_holder_uuid) => String.t(),
          optional(:reclaimed_lease_id) => String.t(),
          optional(:reclaim_failed) => boolean()
        }

  @typedoc """
  What one advisory acquire attempt yielded.

  `:conflict` is *optional*, not merely nilable: both resident GenServers
  short-circuit with a bare two-key map when advisory leasing is switched off
  (`acquire_runtime_lease(%{lease_advisory?: false})` in
  `fleet_finding_emitter.ex` and `forced_release_poller.ex`), and
  `LeaseStarvation.scope_conflict/1` reads the key with `Map.get/2` for exactly
  that reason. The type said `required` until a review caught the drift; there
  is no dialyzer in this repo, so nothing mechanical would have.
  """
  @type scope :: %{
          required(:outcome) => outcome(),
          required(:lease_id) => String.t() | nil,
          optional(:conflict) => conflict() | nil
        }

  @type http_post ::
          (String.t(), map(), [{String.t(), String.t()}], pos_integer() ->
             {:ok, non_neg_integer(), String.t()} | {:error, term()})

  @doc """
  Acquire the Sentinel cycle advisory lease.

  The request shape intentionally matches Python's `lease_advisory_scope/1`
  wrapper around `SentinelAgent.run_cycle/1`.
  """
  @spec acquire_cycle(keyword()) :: scope()
  def acquire_cycle(opts \\ []) do
    body =
      %{
        "surface_id" => Keyword.get(opts, :surface_id, @cycle_surface_id),
        "holder_agent_uuid" => Keyword.get(opts, :holder_agent_uuid, new_holder_uuid()),
        "holder_class" => "process_instance",
        "holder_kind" => Keyword.get(opts, :holder_kind, @cycle_holder_kind),
        "ttl_s" => Keyword.get(opts, :ttl_s, @cycle_ttl_s),
        "intent" => Keyword.get(opts, :intent, @cycle_intent)
      }
      |> maybe_put("audit_session", audit_session(opts))

    acquire_advisory(body, opts)
  end

  @doc false
  @spec acquire_advisory(map(), keyword()) :: scope()
  def acquire_advisory(body, opts \\ []) when is_map(body) do
    surface_id = Map.get(body, "surface_id")

    scope =
      with {:ok, token} <- bearer_token(opts),
           {:ok, status, response_body} <-
             post_acquire_with_recovery(body, token, opts, surface_id) do
        classify_acquire(status, response_body, surface_id)
      else
        {:disabled, reason} ->
          Logger.debug("lease_advisory: disabled #{inspect(reason)}")
          scope(:service_unavailable)

        {:error, reason} ->
          Logger.debug("lease_advisory: acquire failed #{inspect(reason)}")

          # Both transport attempts failed. Either nothing committed, or a
          # lease committed under THIS attempt's holder uuid with both
          # responses lost (2026-08-01: a Postgres stall did exactly that —
          # the stall that causes the timeout is the stall that defeats the
          # single retry). Carry the uuid so the caller can remember it and a
          # later held_by_other naming it can be reclaimed
          # (`maybe_reclaim_own_orphan/3`, `UnitaresSentinel.LeaseReclaim`).
          scope(:service_unavailable, nil, %{
            attempted_holder_uuid: Map.get(body, "holder_agent_uuid")
          })
      end

    case maybe_reclaim_own_orphan(scope, body, opts) do
      # The reclaim re-acquired: the returned scope came out of a full nested
      # acquire_advisory/2 pass, so enforcement has already been applied to
      # it — applying enforce_scope/3 again would clobber the nested pass's
      # `blocked_outcome` with the outer `:enforcement_blocked`.
      {:reacquired, final} -> final
      scope -> enforce_scope(scope, surface_id, opts)
    end
  rescue
    e ->
      Logger.debug("lease_advisory: acquire raised #{inspect(e)}")
      body |> Map.get("surface_id") |> then(&enforce_scope(scope(:client_error), &1, opts))
  catch
    :exit, reason ->
      Logger.debug("lease_advisory: acquire exited #{inspect(reason)}")
      body |> Map.get("surface_id") |> then(&enforce_scope(scope(:client_error), &1, opts))
  end

  @doc """
  Release a previously acquired advisory lease.

  No-op for non-acquire outcomes; all release failures are swallowed.
  """
  @spec release(scope() | String.t() | nil, keyword()) :: :ok
  def release(%{lease_id: nil}, _opts), do: :ok
  def release(nil, _opts), do: :ok

  def release(%{lease_id: lease_id}, opts) when is_binary(lease_id) do
    release(lease_id, opts)
  end

  def release(lease_id, opts) when is_binary(lease_id) do
    body = %{"lease_id" => lease_id, "release_reason" => "normal"}

    with {:ok, token} <- bearer_token(opts),
         {:ok, status, response_body} <- post_json("/v1/lease/release", body, token, opts) do
      log_release(status, response_body, lease_id)
    else
      {:disabled, reason} ->
        Logger.debug("lease_advisory: release skipped #{inspect(reason)}")

      {:error, reason} ->
        Logger.debug("lease_advisory: release failed #{inspect(reason)}")
    end

    :ok
  rescue
    e ->
      Logger.debug("lease_advisory: release raised lease_id=#{lease_id} err=#{inspect(e)}")
      :ok
  catch
    :exit, reason ->
      Logger.debug("lease_advisory: release exited lease_id=#{lease_id} err=#{inspect(reason)}")
      :ok
  end

  @doc """
  The default cycle surface id.

  Public so a resident can name the surface it will be refused on at `init/1`
  time — before any acquire has happened — without hardcoding the literal a
  second time. `ForcedReleasePoller.init/1` reads it into its own `:lease_opts`
  for exactly that reason: `LeaseStarvation.new/1` requires a real surface (the
  starvation sidecar path and the finding fingerprint are both keyed on it), and
  a nil there would collide the poller with `FleetFindingEmitter`.
  """
  @spec cycle_surface_id() :: String.t()
  def cycle_surface_id, do: @cycle_surface_id

  @doc false
  @spec new_holder_uuid() :: String.t()
  def new_holder_uuid do
    <<a::32, b::16, c::16, d::16, e::48>> = :crypto.strong_rand_bytes(16)

    [<<a::32>>, <<b::16>>, <<c::16>>, <<d::16>>, <<e::48>>]
    |> Enum.map_join("-", &Base.encode16(&1, case: :lower))
  end

  defp post_json(path, body, token, opts) do
    http_post = Keyword.get(opts, :http_post, &finch_post/4)
    timeout_ms = Keyword.get(opts, :timeout_ms, lease_plane_timeout_ms())
    url = endpoint_url(Keyword.get(opts, :base_url, lease_plane_base_url()), path)

    http_post.(url, body, headers(token), timeout_ms)
  end

  defp finch_post(url, body, headers, timeout_ms) do
    json = Jason.encode!(body)
    request = Finch.build(:post, url, headers, json)

    case Finch.request(request, UnitaresSentinel.Finch, receive_timeout: timeout_ms) do
      {:ok, %Finch.Response{status: status, body: response_body}} ->
        {:ok, status, response_body}

      {:error, reason} ->
        {:error, reason}
    end
  end

  # A transport error on acquire is ambiguous: the request may never have
  # arrived, or it may have COMMITTED server-side with only the response lost.
  # The second case stranded Sentinel for 24h on 2026-07-29 — the lease row
  # existed, the client never learned its `lease_id`, so its `after` block
  # released a blocked scope instead of the lease, and the lease-plane holder
  # then auto-renewed that orphan every TTL/3 forever (`Reaper.perform` sweeps
  # only `expires_at < now()`, which the renew makes unsatisfiable). Every
  # later tick on that surface saw `held_by_other`.
  #
  # One retry with the SAME body resolves the ambiguity, because acquire is
  # idempotent on `holder_agent_uuid` (RFC §7.13, `Repo.acquire_step/3`): if the
  # first attempt committed, the retry returns that exact lease with its
  # `lease_id`, and the caller can release it normally.
  #
  # SAFETY — why this is not the stable-holder-uuid design that was rejected:
  # the uuid is minted per ACQUIRE ATTEMPT (`acquire_cycle/1` calls
  # `new_holder_uuid/0` while building the body), so only a retry of *this*
  # attempt can adopt *this* lease. Two concurrently live instances still mint
  # different uuids and still contend correctly via `held_by_other`. A uuid made
  # stable across attempts would instead let a second live instance adopt the
  # first one's lease — trading a liveness bug for a double-grant.
  #
  # Bounded to one retry: the failure addressed is a lost response, not a busy
  # server, and this runs inside a resident's tick. Retrying harder would add
  # latency to the path that is already timing out.
  defp post_acquire_with_recovery(body, token, opts, surface_id) do
    case post_json("/v1/lease/acquire", body, token, opts) do
      {:ok, status, response_body} ->
        {:ok, status, response_body}

      {:error, reason} ->
        Logger.debug(
          "lease_advisory: acquire transport error #{inspect(reason)} — retrying once " <>
            "to recover a possibly-committed lease (surface=#{surface_id})"
        )

        case post_json("/v1/lease/acquire", body, token, opts) do
          {:ok, status, response_body} ->
            Logger.info(
              "lease_advisory: acquire recovered after transport error surface=#{surface_id}"
            )

            {:ok, status, response_body}

          {:error, retry_reason} ->
            # Both attempts failed at the transport: either nothing committed,
            # or a lease is stranded whose id this process never learned. The
            # attempt's holder uuid is carried out on the scope (see
            # acquire_advisory/2) so a later held_by_other naming it can be
            # reclaimed; the doctor's immortal_lease check remains the
            # backstop when the resident restarts and forfeits that memory.
            {:error, retry_reason}
        end
    end
  end

  # 2026-08-01 double-lost-response incident: when a conflict names a holder
  # uuid WE minted for an earlier attempt on this surface, the blocking lease
  # is our own — created by an acquire whose response (and whose recovery
  # retry's response) was lost. `holder_agent_uuid` values come from
  # `new_holder_uuid/0` (`:crypto.strong_rand_bytes/1`, process-local), so the
  # match proves authorship: releasing the lease cannot take the surface away
  # from another live holder. Candidates arrive via `opts[:reclaim_candidates]`
  # (threaded by `UnitaresSentinel.LeaseReclaim` from GenServer state).
  #
  # On a successful release the surface is free NOW — re-acquire immediately
  # (fresh uuid, candidates emptied so the nested pass cannot recurse) instead
  # of staying starved until the next tick. If the release fails, keep the
  # held_by_other scope, mark `reclaim_failed`, and let the next tick retry —
  # the candidate list is preserved by `LeaseReclaim.absorb/2`.
  defp maybe_reclaim_own_orphan(
         %{outcome: :held_by_other, conflict: %{held_by_uuid: held_by, blocking_lease_id: blocking}} =
           scope,
         body,
         opts
       )
       when is_binary(held_by) and is_binary(blocking) do
    candidates = Keyword.get(opts, :reclaim_candidates, [])

    if held_by in candidates do
      surface_id = Map.get(body, "surface_id")

      Logger.warning(
        "lease_advisory: held_by_other names our own prior attempt — reclaiming lease " <>
          "stranded by a lost acquire response (surface=#{surface_id} " <>
          "lease_id=#{blocking} holder_uuid=#{held_by})"
      )

      case release_checked(blocking, opts) do
        :ok ->
          retry_body = Map.put(body, "holder_agent_uuid", new_holder_uuid())
          final = acquire_advisory(retry_body, Keyword.put(opts, :reclaim_candidates, []))
          {:reacquired, put_in_conflict(final, :reclaimed_lease_id, blocking)}

        {:error, reason} ->
          Logger.warning(
            "lease_advisory: reclaim release failed lease_id=#{blocking} " <>
              "#{inspect(reason)} — will retry next tick"
          )

          put_in_conflict(scope, :reclaim_failed, true)
      end
    else
      scope
    end
  end

  defp maybe_reclaim_own_orphan(scope, _body, _opts), do: scope

  defp put_in_conflict(scope, key, value) do
    conflict = (Map.get(scope, :conflict) || %{}) |> Map.put(key, value)
    Map.put(scope, :conflict, conflict)
  end

  @reclaim_release_reason "reclaimed_lost_acquire"

  # Unlike `release/2` (fire-and-forget, swallows everything), a reclaim
  # release must report whether it worked: on failure the caller keeps its
  # candidate memory and retries next tick instead of assuming the orphan is
  # gone. The distinct release_reason keeps `release_reason='normal'` honest
  # as "a live holder released its own in-hand lease" — the property the 90d
  # legitimate-long-hold analysis rests on — while a reclaimed orphan's
  # span-since-acquire is anything but a legitimate hold.
  defp release_checked(lease_id, opts) do
    case post_release(lease_id, @reclaim_release_reason, opts) do
      {:ok, %{"ok" => true}} ->
        Logger.info(
          "lease_advisory: released reclaimed lease_id=#{lease_id} " <>
            "reason=#{@reclaim_release_reason}"
        )

        :ok

      # Fall back to 'normal' on ANY failed first attempt, not only an
      # explicit schema_invalid: a plane whose router predates the reason
      # 422s, but a plane with new router code and the 056 migration
      # unapplied fails the CHECK constraint and surfaces as a 503 — and a
      # transport blip looks like neither. One 'normal' retry covers all
      # three without depending on deploy order; the audit distinction
      # upgrades itself once the plane is current. Releasing twice is safe
      # (release is a WHERE released_at IS NULL update), and if both fail the
      # caller keeps its candidate memory and retries next tick.
      first_failure ->
        case post_release(lease_id, "normal", opts) do
          {:ok, %{"ok" => true}} ->
            Logger.info(
              "lease_advisory: released reclaimed lease_id=#{lease_id} reason=normal " <>
                "(#{@reclaim_release_reason} attempt failed: #{inspect(first_failure)}; " <>
                "deploy-order fallback)"
            )

            :ok

          other ->
            {:error, other}
        end
    end
  end

  defp post_release(lease_id, reason, opts) do
    with {:ok, token} <- bearer_token(opts),
         {:ok, _status, response_body} <-
           post_json(
             "/v1/lease/release",
             %{"lease_id" => lease_id, "release_reason" => reason},
             token,
             opts
           ) do
      decode_object(response_body)
    end
  end

  defp classify_acquire(status, response_body, surface_id) do
    case decode_object(response_body) do
      {:ok, %{"ok" => true, "lease" => %{"lease_id" => lease_id}} = decoded} ->
        outcome =
          if Map.get(decoded, "idempotent", false), do: :acquired_idempotent, else: :acquired_new

        Logger.info("lease_advisory: #{outcome} surface=#{surface_id} lease_id=#{lease_id}")
        scope(outcome, lease_id)

      {:ok, %{"ok" => false, "error" => "held_by_other"} = decoded} ->
        Logger.info(
          "lease_advisory: held_by_other surface=#{surface_id} held_by=#{Map.get(decoded, "held_by_uuid")} lease_id=#{Map.get(decoded, "blocking_lease_id")} (Phase A: proceeding regardless)"
        )

        # Carry `blocking_lease_id` through instead of discarding it one line
        # after logging. It is the force-release argument
        # (`POST /v1/lease/force-release {"lease_id": ...}`), which is what turns
        # a lease-starvation self-finding from "something is wrong" into
        # "run this". 2026-07-31 immortal-lease incident.
        scope(:held_by_other, nil, %{
          blocking_lease_id: Map.get(decoded, "blocking_lease_id"),
          held_by_uuid: Map.get(decoded, "held_by_uuid"),
          expires_at: Map.get(decoded, "expires_at")
        })

      {:ok, %{"ok" => false, "error" => "permission_denied", "reason" => reason}} ->
        Logger.warning("lease_advisory: permission_denied surface=#{surface_id} reason=#{reason}")
        scope(:permission_denied)

      {:ok, %{"ok" => false, "error" => "schema_invalid", "detail" => detail}} ->
        Logger.warning(
          "lease_advisory: schema_invalid surface=#{surface_id} detail=#{inspect(detail)}"
        )

        scope(:schema_invalid)

      {:ok, %{"ok" => false, "error" => "service_unavailable"}} ->
        Logger.info("lease_advisory: service_unavailable surface=#{surface_id}")
        scope(:service_unavailable)

      {:ok, _payload} ->
        scope(:client_error)

      {:error, _detail} when status in [401, 403] ->
        Logger.warning("lease_advisory: permission_denied surface=#{surface_id} status=#{status}")
        scope(:permission_denied)

      {:error, _detail} when is_integer(status) and status >= 400 ->
        Logger.info("lease_advisory: service_unavailable surface=#{surface_id} status=#{status}")
        scope(:service_unavailable)

      {:error, _detail} ->
        scope(:schema_invalid)
    end
  end

  defp log_release(status, response_body, lease_id) do
    case decode_object(response_body) do
      {:ok, %{"ok" => true}} ->
        Logger.info("lease_advisory: released lease_id=#{lease_id} ok=true")

      {:ok, payload} ->
        Logger.debug(
          "lease_advisory: release non-ok lease_id=#{lease_id} status=#{status} body=#{inspect(payload)}"
        )

      {:error, detail} ->
        Logger.debug(
          "lease_advisory: release invalid response lease_id=#{lease_id} status=#{status} detail=#{inspect(detail)}"
        )
    end
  end

  defp decode_object(response_body) when is_binary(response_body) do
    case Jason.decode(response_body) do
      {:ok, %{} = decoded} -> {:ok, decoded}
      {:ok, _} -> {:error, "response was not an object"}
      {:error, _} -> {:error, "response was not JSON"}
    end
  end

  defp scope(outcome, lease_id \\ nil, conflict \\ nil),
    do: %{outcome: outcome, lease_id: lease_id, conflict: conflict}

  defp headers(token) do
    [
      {"Authorization", "Bearer #{token}"},
      {"Accept", "application/json"},
      {"Content-Type", "application/json"}
    ]
  end

  defp bearer_token(opts) do
    opts
    |> Keyword.get_lazy(:bearer_token, fn -> System.get_env("LEASE_PLANE_BEARER_TOKEN") end)
    |> case do
      token when is_binary(token) ->
        case String.trim(token) do
          "" -> {:disabled, :missing_bearer_token}
          trimmed -> {:ok, trimmed}
        end

      _ ->
        {:disabled, :missing_bearer_token}
    end
  end

  defp endpoint_url(base_url, path) do
    String.trim_trailing(base_url, "/") <> path
  end

  defp lease_plane_base_url do
    Application.get_env(:unitares_sentinel, :lease_plane_base_url) ||
      System.get_env("LEASE_PLANE_BASE_URL") ||
      @default_base_url
  end

  defp lease_plane_timeout_ms do
    Application.get_env(:unitares_sentinel, :lease_plane_timeout_ms, @default_timeout_ms)
  end

  defp audit_session(opts) do
    non_empty_string(Keyword.get(opts, :audit_session)) ||
      configured_audit_session() ||
      session_anchor_client_session_id(opts)
  end

  defp configured_audit_session do
    non_empty_string(Application.get_env(:unitares_sentinel, :lease_audit_session)) ||
      non_empty_string(System.get_env("UNITARES_SENTINEL_AUDIT_SESSION"))
  end

  defp enforce_scope(%{lease_id: nil, outcome: outcome} = scope, surface_id, opts) do
    if surface_enforced?(surface_id, opts) do
      Logger.warning("lease_enforcement: blocked surface=#{surface_id} outcome=#{outcome}")

      # `:enforcement_blocked` is a conflation of held_by_other,
      # permission_denied, schema_invalid, client_error AND a missing/blank
      # LEASE_PLANE_BEARER_TOKEN. Overwriting `:outcome` destroyed the only
      # record of *why*, and a caller that cannot tell "another holder" from
      # "the plane is down" can only ever emit a remedy that is right by luck.
      # Keep the pre-enforcement outcome and stamp the surface — the poller
      # passes no `:lease_opts`, so this is its only channel for knowing which
      # surface it was refused on. 2026-07-31 immortal-lease incident.
      conflict =
        (Map.get(scope, :conflict) || %{})
        |> Map.put(:blocked_outcome, outcome)
        |> Map.put(:surface_id, surface_id)

      %{scope | outcome: :enforcement_blocked, conflict: conflict}
    else
      scope
    end
  end

  defp enforce_scope(scope, _surface_id, _opts), do: scope

  defp surface_enforced?(surface_id, opts) when is_binary(surface_id) do
    kinds = Keyword.get_lazy(opts, :enforced_surface_kinds, &configured_enforced_surface_kinds/0)
    "*" in kinds or surface_kind(surface_id) in kinds
  end

  defp surface_enforced?(_surface_id, _opts), do: false

  defp configured_enforced_surface_kinds do
    configured =
      Application.get_env(:unitares_sentinel, :lease_enforced_surface_kinds) ||
        System.get_env("LEASE_PLANE_ENFORCED_SURFACE_KINDS") ||
        ""

    configured
    |> split_surface_kinds()
    |> MapSet.new()
  end

  defp split_surface_kinds(value) when is_binary(value) do
    value
    |> String.split(",")
    |> Enum.map(&String.trim/1)
    |> Enum.reject(&(&1 == ""))
  end

  defp split_surface_kinds(values) when is_list(values), do: values
  defp split_surface_kinds(_value), do: []

  defp surface_kind(surface_id), do: surface_id |> String.split(":", parts: 2) |> hd()

  defp session_anchor_client_session_id(opts) do
    case Keyword.fetch(opts, :anchor) do
      {:ok, %{} = anchor} ->
        non_empty_string(Map.get(anchor, "client_session_id"))

      :error ->
        case UnitaresSentinel.SessionAnchor.load() do
          {:ok, anchor} -> non_empty_string(Map.get(anchor, "client_session_id"))
          {:error, _reason} -> nil
        end
    end
  end

  defp non_empty_string(value) when is_binary(value) do
    case String.trim(value) do
      "" -> nil
      trimmed -> trimmed
    end
  end

  defp non_empty_string(_value), do: nil

  defp maybe_put(map, _key, nil), do: map
  defp maybe_put(map, _key, ""), do: map
  defp maybe_put(map, key, value), do: Map.put(map, key, value)
end
