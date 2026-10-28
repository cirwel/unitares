defmodule Portal.FakeAuth do
  @moduledoc "A test backend: the session's token decides."
  @behaviour Portal.Auth

  @impl true
  def verify(%{"token" => "valid"}), do: {:ok, %{label: "operator"}}
  def verify(%{"token" => "expired"}), do: {:error, :expired}
  def verify(_session), do: {:error, :no_session}
end
