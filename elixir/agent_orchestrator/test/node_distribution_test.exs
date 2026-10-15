defmodule AgentOrchestrator.NodeDistributionTest do
  use ExUnit.Case, async: true

  alias AgentOrchestrator.NodeDistribution

  test "no real-home variable means an unnamed node" do
    assert NodeDistribution.plan(%{}) == :unnamed
    assert NodeDistribution.plan(%{"AGENT_ORCHESTRATOR_NODE_REAL_HOME" => ""}) == :unnamed
  end

  test "a real-home variable names the node, with the default or the configured sname" do
    assert NodeDistribution.plan(%{"AGENT_ORCHESTRATOR_NODE_REAL_HOME" => "/home/op"}) ==
             {:named, "agent-orchestrator", "/home/op"}

    assert NodeDistribution.plan(%{
             "AGENT_ORCHESTRATOR_NODE_REAL_HOME" => "/home/op",
             "AGENT_ORCHESTRATOR_NODE_SNAME" => "other"
           }) == {:named, "other", "/home/op"}
  end
end
