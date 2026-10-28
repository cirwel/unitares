defmodule PortalWeb.HomeLive do
  @moduledoc "The hub's home screen. The timeline moves here with the sign-in PR."
  use PortalWeb, :live_view

  @impl true
  def render(assigns) do
    ~H"""
    <main>
      <h1>UNITARES hub</h1>
      <p>Signed in. The timeline arrives with the next step.</p>
    </main>
    """
  end
end
