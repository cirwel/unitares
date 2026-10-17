defmodule UnitaresLeasePlane.NodeDistributionTest do
  use ExUnit.Case, async: true

  alias UnitaresLeasePlane.NodeDistribution

  test "no real-home variable means an unnamed node" do
    assert NodeDistribution.plan(%{}) == :unnamed
    assert NodeDistribution.plan(%{"LEASE_PLANE_NODE_REAL_HOME" => ""}) == :unnamed
  end

  test "a real-home variable names the node, with the default or the configured sname" do
    assert NodeDistribution.plan(%{"LEASE_PLANE_NODE_REAL_HOME" => "/home/op"}) ==
             {:named, "unitares-lease-plane", "/home/op"}

    assert NodeDistribution.plan(%{
             "LEASE_PLANE_NODE_REAL_HOME" => "/home/op",
             "LEASE_PLANE_NODE_SNAME" => "other"
           }) == {:named, "other", "/home/op"}
  end
end
