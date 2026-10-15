defmodule AgentOrchestrator.NodeDistributionTest do
  use ExUnit.Case, async: false

  alias AgentOrchestrator.NodeDistribution

  test "no cookie means an unnamed node" do
    assert NodeDistribution.plan(%{}) == :unnamed
    assert NodeDistribution.plan(%{"AGENT_ORCHESTRATOR_NODE_COOKIE" => ""}) == :unnamed
  end

  test "a cookie names the node, with the default or the configured sname" do
    assert NodeDistribution.plan(%{"AGENT_ORCHESTRATOR_NODE_COOKIE" => "c1"}) ==
             {:named, "agent-orchestrator", "c1"}

    assert NodeDistribution.plan(%{
             "AGENT_ORCHESTRATOR_NODE_COOKIE" => "c1",
             "AGENT_ORCHESTRATOR_NODE_SNAME" => "other"
           }) == {:named, "other", "c1"}
  end

  test "the cookie variable is removed from the environment" do
    System.put_env("AGENT_ORCHESTRATOR_NODE_COOKIE", "")
    NodeDistribution.start_from_env()
    assert System.get_env("AGENT_ORCHESTRATOR_NODE_COOKIE") == nil
  end
end
