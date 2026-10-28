defmodule Portal.Auth do
  @moduledoc """
  Whether a request carries a signed-in operator (portal-hub-v0 §3.2).

  The backend is `config :portal, :auth`. The default, `Portal.Auth.Unconfigured`,
  refuses everyone: the sign-in handoff to the governance server's passkey
  login is its own pull request, reviewed on the auth path. Until it lands,
  the hub serves nothing behind its guard.
  """

  @callback verify(session :: map()) :: {:ok, map()} | {:error, atom()}

  @doc "`{:ok, principal}` or `{:error, reason}` for a session map."
  def verify(session) when is_map(session), do: backend().verify(session)
  def verify(_session), do: {:error, :no_session}

  defp backend, do: Application.get_env(:portal, :auth, Portal.Auth.Unconfigured)
end

defmodule Portal.Auth.Unconfigured do
  @moduledoc "No sign-in is built yet, so no session is ever accepted."
  @behaviour Portal.Auth

  @impl true
  def verify(_session), do: {:error, :not_configured}
end
