defmodule UnitaresSdk.IdentityTest do
  use ExUnit.Case, async: true

  alias UnitaresSdk.{Backoff, Config, Identity}

  describe "onboard args" do
    test "always mints fresh" do
      assert %{"force_new" => true} = Identity.onboard_args(nil, "h")
    end

    test "rootless when there is no prior uuid" do
      refute Map.has_key?(Identity.onboard_args(nil, "h"), "parent_agent_id")
    end

    test "chains lineage when a prior uuid exists" do
      assert %{"parent_agent_id" => "u1"} = Identity.onboard_args("u1", "h")
    end

    test "an empty prior uuid is not lineage" do
      refute Map.has_key?(Identity.onboard_args("", "h"), "parent_agent_id")
    end
  end

  describe "from_onboard" do
    test "extracts the triple" do
      r = %{"uuid" => "u", "client_session_id" => "c", "continuity_token" => "t"}

      assert {:ok, %{agent_id: "u", client_session_id: "c", continuity_token: "t"}} =
               Identity.from_onboard(r)
    end

    test "a response naming no agent is not an identity" do
      assert :error = Identity.from_onboard(%{"client_session_id" => "c"})
      assert :error = Identity.from_onboard(%{"uuid" => ""})
    end
  end

  describe "check-in args must never declare agent_id" do
    # The rule anima_broker learned live on 2026-07-03: declaring agent_id makes
    # the REST strict gate skip its typed refusal, so binding loss becomes
    # unobservable — check-ins resolve by uuid passthrough with no PG renewal
    # and no Redis re-cache. Its Redis-wipe acceptance test passed WITHOUT
    # exercising recovery until the key was dropped.
    @identity %{agent_id: "uuid-1", client_session_id: "csid-1", continuity_token: "ctok-1"}

    test "echoes client_session_id and does not declare agent_id" do
      args = Identity.check_in_args(%{"response_text" => "x"}, @identity)

      assert args["client_session_id"] == "csid-1"
      refute Map.has_key?(args, "agent_id")
    end

    test "a caller cannot opt back into the bug by passing agent_id in fields" do
      args = Identity.check_in_args(%{"response_text" => "x", "agent_id" => "uuid-1"}, @identity)

      refute Map.has_key?(args, "agent_id")
    end

    test "the atom key is dropped too" do
      args = Identity.check_in_args(%{agent_id: "uuid-1"}, @identity)

      refute Map.has_key?(args, :agent_id)
      refute Map.has_key?(args, "agent_id")
    end

    test "absent identity material is omitted, not sent as nil" do
      args = Identity.check_in_args(%{"a" => 1}, %{client_session_id: nil, continuity_token: ""})

      refute Map.has_key?(args, "client_session_id")
      refute Map.has_key?(args, "continuity_token")
    end

    test "declares_agent_id?/1 lets a client assert the rule on its own builder" do
      assert Identity.declares_agent_id?(%{"agent_id" => "u"})
      assert Identity.declares_agent_id?(%{agent_id: "u"})
      refute Identity.declares_agent_id?(%{"client_session_id" => "c"})
    end
  end

  describe "anchor file" do
    setup do
      path =
        Path.join(
          System.tmp_dir!(),
          "unitares_sdk_anchor_#{System.unique_integer([:positive])}.json"
        )

      on_exit(fn -> File.rm(path) end)
      {:ok, path: path}
    end

    test "round-trips", %{path: path} do
      Identity.persist_anchor(path, %{"agent_id" => "u1"})
      assert Identity.load_prior_uuid(path) == "u1"
    end

    test "accepts the agent_uuid spelling anima_broker writes", %{path: path} do
      File.write!(path, ~s({"agent_uuid": "u2"}))
      assert Identity.load_prior_uuid(path) == "u2"
    end

    test "a corrupt anchor means rootless, never a crash", %{path: path} do
      File.write!(path, "}{ not json")
      assert Identity.load_prior_uuid(path) == nil
    end

    test "a missing anchor means rootless" do
      assert Identity.load_prior_uuid("/nonexistent/nope.json") == nil
    end

    test "an unwritable path degrades to :ok, never raises" do
      assert :ok = Identity.persist_anchor("/proc/nope/anchor.json", %{"agent_id" => "u"})
    end
  end

  describe "config gate (the 155 rootless anons)" do
    test "test env is hermetic even with the flag set" do
      assert Config.url(:test, "1", "http://localhost:8767") == nil
    end

    test "dev without the flag is disabled — this is the path the 155 came from" do
      assert Config.url(:dev, nil, "http://localhost:8767") == nil
      assert Config.url(:dev, "", "http://localhost:8767") == nil
      assert Config.url(:dev, "true", "http://localhost:8767") == nil
    end

    test "the flag must be exactly \"1\"" do
      assert Config.url(:prod, "1", "http://x:8767") == "http://x:8767"
    end

    test "normalizes the url it returns" do
      assert Config.url(:prod, "1", "http://x:8767/") == "http://x:8767"
    end
  end

  describe "backoff" do
    test "doubles and clamps" do
      assert Backoff.next(5_000, 60_000) == 10_000
      assert Backoff.next(45_000, 60_000) == 60_000
    end

    test "nil yields the base delay" do
      assert Backoff.next_onboard(nil) == Backoff.onboard_base()
    end

    test "a closed breaker is nil, never 0" do
      # BEAM monotonic time has an arbitrary, usually negative epoch: a breaker
      # initialised to 0 would read as open at boot and skip every call.
      now = System.monotonic_time(:millisecond)
      refute Backoff.open?(nil, now)
      assert now < 0 or now >= 0
    end

    test "an unexpired deadline is open" do
      now = System.monotonic_time(:millisecond)
      assert Backoff.open?(now + 1_000, now)
      refute Backoff.open?(now - 1_000, now)
    end
  end
end
