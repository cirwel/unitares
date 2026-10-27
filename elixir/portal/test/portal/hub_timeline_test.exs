defmodule Portal.HubTimelineTest do
  @moduledoc """
  What the hub's copy of the timeline adds (portal-hub-v0 H2d-2b): both of its
  clients read with the hub's own read-only key (operator decision 2026-10-27),
  the key stays out of anything inspected, and the timeline starts only with
  that key.
  """
  use PortalWeb.ConnCase, async: false

  @moduletag :capture_log
  @key String.duplicate("k", 40)
  @config_dir Path.expand("../../config", __DIR__)

  setup do
    saved = Application.get_env(:portal, :governance)
    on_exit(fn -> Application.put_env(:portal, :governance, saved) end)
    :ok
  end

  describe "the timeline client" do
    test "sends the read-only key as a bearer" do
      Application.put_env(:portal, :governance,
        timeline_url: "http://gov.test/v1/timeline",
        read_token: @key
      )

      test = self()

      Req.Test.stub(Portal.Governance, fn conn ->
        send(test, {:authorization, Plug.Conn.get_req_header(conn, "authorization")})
        Req.Test.json(conn, %{"events" => [], "agents" => %{}})
      end)

      assert {:ok, _} =
               Portal.Governance.timeline([hours: 1],
                 req_options: [plug: {Req.Test, Portal.Governance}]
               )

      assert_received {:authorization, ["Bearer " <> @key]}
    end

    test "sends no credential when it has none" do
      Application.put_env(:portal, :governance, timeline_url: "http://gov.test/v1/timeline")
      test = self()

      Req.Test.stub(Portal.Governance, fn conn ->
        send(test, {:authorization, Plug.Conn.get_req_header(conn, "authorization")})
        Req.Test.json(conn, %{"events" => [], "agents" => %{}})
      end)

      Portal.Governance.timeline([hours: 1], req_options: [plug: {Req.Test, Portal.Governance}])
      assert_received {:authorization, []}
    end

    test "has nothing but the timeline read" do
      exported =
        Portal.Governance.__info__(:functions) |> Keyword.keys() |> Enum.uniq() |> Enum.sort()

      assert exported == [:config, :timeline]
    end
  end

  describe "the stream client" do
    test "sends the read-only key on the upgrade" do
      url = Portal.Test.FakeGovernanceWS.start!(test: self())
      pubsub = :"hub_timeline_pubsub_#{System.unique_integer([:positive])}"
      start_supervised!({Phoenix.PubSub, name: pubsub})

      start_supervised!(
        {Portal.Firehose,
         url: url,
         read_token: @key,
         pubsub: pubsub,
         name: :"hub_firehose_#{System.unique_integer([:positive])}"}
      )

      assert_receive {:fake_ws_authorization, ["Bearer " <> @key]}, 2_000
    end

    test "never shows the key when its state is inspected" do
      state = %Portal.Firehose{url: "ws://127.0.0.1:1/ws/eisv", read_token: @key}
      refute inspect(state) =~ @key
      assert inspect(state) =~ "[redacted]"
    end
  end

  describe "the home screen" do
    import Phoenix.LiveViewTest

    test "is the timeline, and says why it is not updating when the hub has no key", %{conn: conn} do
      saved = Application.get_env(:portal, :auth)
      Application.put_env(:portal, :auth, Portal.FakeAuth)
      Application.put_env(:portal, :governance, [])

      on_exit(fn ->
        if saved,
          do: Application.put_env(:portal, :auth, saved),
          else: Application.delete_env(:portal, :auth)
      end)

      conn = Plug.Test.init_test_session(conn, token: "valid")
      {:ok, view, html} = live(conn, "/")
      assert html =~ "Timeline — live"
      assert has_element?(view, "#signed-in a[href='/signout']")
      assert html =~ "the hub has no read-only key, PORTAL_READ_TOKEN"
    end
  end

  describe "configuration" do
    test "the timeline starts only with the hub's read-only key" do
      vars = ~w(PORTAL_SECRET_KEY_BASE PORTAL_READ_TOKEN)
      saved = Map.new(vars, &{&1, System.get_env(&1)})

      read = fn ->
        Config.Reader.read!(Path.join(@config_dir, "runtime.exs"), env: :prod, target: :host)
      end

      try do
        System.put_env("PORTAL_SECRET_KEY_BASE", String.duplicate("s", 64))
        System.delete_env("PORTAL_READ_TOKEN")
        timeline = get_in(read.(), [:portal, :timeline])
        refute timeline[:start_timeline_feed]
        refute timeline[:start_firehose]

        System.put_env("PORTAL_READ_TOKEN", @key)
        cfg = read.()
        assert get_in(cfg, [:portal, :timeline, :start_timeline_feed])
        assert get_in(cfg, [:portal, :timeline, :start_firehose])
        assert get_in(cfg, [:portal, :governance, :read_token]) == @key
      after
        Enum.each(saved, fn
          {k, nil} -> System.delete_env(k)
          {k, v} -> System.put_env(k, v)
        end)
      end
    end
  end
end
