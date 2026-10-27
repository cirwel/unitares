defmodule Portal.SwitchAuth do
  @moduledoc "A test backend whose verdict a test can flip: `config :portal, :switch_auth_ok`."
  @behaviour Portal.Auth

  @impl true
  def verify(_session) do
    if Application.get_env(:portal, :switch_auth_ok, false),
      do: {:ok, %{label: "operator"}},
      else: {:error, :signed_out}
  end
end
