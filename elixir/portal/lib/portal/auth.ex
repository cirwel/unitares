defmodule Portal.Auth do
  @moduledoc """
  Whether a request carries a signed-in operator (portal-hub-v0 §3.2).

  The backend is `config :portal, :auth`. The default, `Portal.Auth.Unconfigured`,
  refuses everyone. `Portal.Auth.Hub` asks the governance server about the
  session's hub handle (`Portal.Gov.check/1`); the runtime config selects it
  once the handoff is configured.
  """

  @callback verify(session :: map()) :: {:ok, map()} | {:error, atom()}

  @doc "`{:ok, principal}` or `{:error, reason}` for a session map."
  def verify(session) when is_map(session), do: backend().verify(session)
  def verify(_session), do: {:error, :no_session}

  defp backend, do: Application.get_env(:portal, :auth, Portal.Auth.Unconfigured)
end

defmodule Portal.Auth.Unconfigured do
  @moduledoc "No sign-in is configured, so no session is ever accepted."
  @behaviour Portal.Auth

  @impl true
  def verify(_session), do: {:error, :not_configured}
end

defmodule Portal.Auth.Hub do
  @moduledoc """
  A session is signed in while the governance server says its hub handle is
  live. Every call asks the server, over its owner-only socket: there is no
  cache, so signing the dashboard out, or revoking its passkey, takes effect
  on the hub's next request.
  """
  @behaviour Portal.Auth

  @impl true
  def verify(%{"hub_handle" => handle}) when is_binary(handle) do
    case Portal.Gov.check(handle) do
      {:ok, body} ->
        {:ok,
         %{
           label: body["operator_label"],
           fresh: body["fresh"] == true,
           authenticated_at: body["authenticated_at"],
           expires_at: body["expires_at"]
         }}

      {:error, reason} ->
        {:error, reason}
    end
  end

  def verify(_session), do: {:error, :no_session}
end
