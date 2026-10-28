defmodule PortalWeb.HomeLive do
  @moduledoc "The hub's home screen. The timeline moves here in the next step (H2d)."
  use PortalWeb, :live_view

  @impl true
  def render(assigns) do
    ~H"""
    <main>
      <h1>UNITARES hub</h1>
      <p>Signed in as {@operator.label}. The timeline arrives with the next step.</p>
      <p><a href="/signout">Sign out</a></p>
    </main>
    """
  end
end
