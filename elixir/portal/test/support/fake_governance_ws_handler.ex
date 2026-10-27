defmodule Portal.Test.FakeGovernanceWSHandler do
  @moduledoc false
  @behaviour WebSock

  @impl true
  def init(state) do
    send(state.test, {:fake_ws_up, self()})
    {:ok, state}
  end

  @impl true
  def handle_in(_frame, state), do: {:ok, state}

  @impl true
  def handle_info({:push, text}, state), do: {:push, {:text, text}, state}
  def handle_info(:close, state), do: {:stop, :normal, state}
  def handle_info(_message, state), do: {:ok, state}
end
