defmodule UnitaresLeasePlane.PublishedDefaultTest do
  use ExUnit.Case, async: true

  alias UnitaresLeasePlane.Application, as: LeaseApplication

  @compose Path.expand("../../../docker-compose.yml", __DIR__)

  # The default docker-compose.yml publishes for the bearer, read from the file
  # itself so a rotated default with a stale digest fails here.
  defp published_default do
    [_, value] = Regex.run(~r/\$\{LEASE_PLANE_BEARER_TOKEN:-([^}]*)\}/, File.read!(@compose))
    value
  end

  test "the pinned digest matches the compose default" do
    digest = Base.encode16(:crypto.hash(:sha256, published_default()), case: :lower)
    assert digest == LeaseApplication.published_bearer_sha256()
  end

  test "the published default warns and names the variable, never the value" do
    [warning] = LeaseApplication.published_default_warnings(published_default(), nil, true)
    assert warning =~ "LEASE_PLANE_BEARER_TOKEN"
    refute warning =~ published_default()
  end

  test "own, blank and unset tokens warn about nothing" do
    assert LeaseApplication.published_default_warnings("operator-chosen", "0.0.0.0", false) == []
    assert LeaseApplication.published_default_warnings("  ", "0.0.0.0", false) == []
    assert LeaseApplication.published_default_warnings(nil, "0.0.0.0", false) == []
  end

  test "a bind beyond loopback outside a container adds a second warning" do
    assert [_, exposed] =
             LeaseApplication.published_default_warnings(published_default(), "0.0.0.0", false)

    assert exposed =~ "UNITARES_LEASE_PLANE_HTTP_IP"
    assert exposed =~ "config :lease_plane, http_ip:"
  end

  test "the :any atom binds every interface; :loopback does not" do
    assert [_, _] = LeaseApplication.published_default_warnings(published_default(), :any, false)

    assert [_] =
             LeaseApplication.published_default_warnings(published_default(), :loopback, false)
  end

  test "the compose container's every-interface bind does not add it" do
    assert [_] = LeaseApplication.published_default_warnings(published_default(), "0.0.0.0", true)
  end

  test "a bind set in app config counts, not only the environment variable" do
    assert [_, _] =
             LeaseApplication.published_default_warnings(published_default(), {0, 0, 0, 0}, false)

    assert [_] =
             LeaseApplication.published_default_warnings(
               published_default(),
               {127, 0, 0, 1},
               false
             )
  end

  test "loopback binds do not add it" do
    for ip <- [nil, "", "127.0.0.1", "::1", "::ffff:127.0.0.1", "::ffff:127.255.0.9"] do
      assert [_] = LeaseApplication.published_default_warnings(published_default(), ip, false)
    end
  end

  test "a Unix socket bind has no port, so it does not add it" do
    assert [_] =
             LeaseApplication.published_default_warnings(
               published_default(),
               {:local, "/run/lease.sock"},
               false
             )
  end

  test "an IPv4-mapped address beyond loopback still adds it" do
    assert [_, _] =
             LeaseApplication.published_default_warnings(
               published_default(),
               "::ffff:10.0.0.5",
               false
             )
  end
end

defmodule UnitaresLeasePlane.ResolvedHttpIpTest do
  # Mutates the lease_plane app env and the process environment.
  use ExUnit.Case, async: false

  alias UnitaresLeasePlane.Application, as: LeaseApplication

  setup do
    env = System.get_env("UNITARES_LEASE_PLANE_HTTP_IP")
    token = System.get_env("LEASE_PLANE_BEARER_TOKEN")
    config = Application.fetch_env(:lease_plane, :http_ip)
    System.delete_env("UNITARES_LEASE_PLANE_HTTP_IP")

    on_exit(fn ->
      if env,
        do: System.put_env("UNITARES_LEASE_PLANE_HTTP_IP", env),
        else: System.delete_env("UNITARES_LEASE_PLANE_HTTP_IP")

      if token,
        do: System.put_env("LEASE_PLANE_BEARER_TOKEN", token),
        else: System.delete_env("LEASE_PLANE_BEARER_TOKEN")

      case config do
        {:ok, value} -> Application.put_env(:lease_plane, :http_ip, value)
        :error -> Application.delete_env(:lease_plane, :http_ip)
      end
    end)
  end

  # The boot path judges this value, so a bind set only in app config must reach it.
  test "with the variable unset, the app-config bind is the one judged" do
    Application.put_env(:lease_plane, :http_ip, {0, 0, 0, 0})
    assert LeaseApplication.resolved_http_ip() == {0, 0, 0, 0}
  end

  test "the variable wins over app config" do
    Application.put_env(:lease_plane, :http_ip, {0, 0, 0, 0})
    System.put_env("UNITARES_LEASE_PLANE_HTTP_IP", "127.0.0.1")
    assert LeaseApplication.resolved_http_ip() == {127, 0, 0, 1}
  end

  test "the boot warnings judge an app-config bind, not only the variable" do
    [_, value] =
      Regex.run(
        ~r/\$\{LEASE_PLANE_BEARER_TOKEN:-([^}]*)\}/,
        File.read!(Path.expand("../../../docker-compose.yml", __DIR__))
      )

    System.put_env("LEASE_PLANE_BEARER_TOKEN", value)
    Application.put_env(:lease_plane, :http_ip, {0, 0, 0, 0})

    if File.exists?("/.dockerenv") do
      assert [_] = LeaseApplication.boot_warnings()
    else
      assert [_, _] = LeaseApplication.boot_warnings()
    end
  end

  test "an unparseable variable reads as unknown, not as exposed" do
    System.put_env("UNITARES_LEASE_PLANE_HTTP_IP", "not-an-address")
    assert LeaseApplication.resolved_http_ip() == nil
  end
end
