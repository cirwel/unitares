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
  end

  test "the compose container's every-interface bind does not add it" do
    assert [_] = LeaseApplication.published_default_warnings(published_default(), "0.0.0.0", true)
  end

  test "loopback binds do not add it" do
    for ip <- [nil, "", "127.0.0.1", "::1"] do
      assert [_] = LeaseApplication.published_default_warnings(published_default(), ip, false)
    end
  end
end
