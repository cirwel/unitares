defmodule UnitaresSdk.Config do
  @moduledoc """
  Opt-in resolution of the governance URL.

  Onboarding the live substrate is **opt-in**, not "the URL happens to be set."
  This is a bug-shaped rule, not a preference: on 2026-06-18 a dev shell that
  sourced a `.env` carrying the governance URL and then ran `mix run bench/…`
  or `mix test` booted a governance GenServer and onboarded a fresh `force_new`
  identity against the LIVE substrate on every boot. **155 rootless `anon`
  one-shots** accumulated through that path before anyone noticed.

  A `:test`-only guard is not enough — that leaves `:dev` bench and load-gen
  wide open, which is exactly where the 155 came from. The gate is therefore:

    * `:test` is **always** hermetic, even with the flag set;
    * every other env requires the flag to be exactly `"1"`.

  The launcher exports the flag *after* sourcing `.env`, so the flag cannot
  leak through `.env` into a developer shell.
  """

  @doc """
  Resolve the governance base URL, or `nil` when governance is disabled.

  ## Examples

      iex> UnitaresSdk.Config.url(:test, "1", "http://localhost:8767")
      nil

      iex> UnitaresSdk.Config.url(:prod, "1", "http://localhost:8767")
      "http://localhost:8767"

      iex> UnitaresSdk.Config.url(:dev, nil, "http://localhost:8767")
      nil
  """
  @spec url(atom(), String.t() | nil, String.t() | nil) :: String.t() | nil
  def url(:test, _flag, _env_url), do: nil
  def url(_config_env, "1", env_url), do: UnitaresSdk.Transport.normalize_url(env_url)
  def url(_config_env, _flag, _env_url), do: nil

  @doc """
  Resolve from the process environment. `flag_var` defaults to the fleet
  convention; pass your app's own if it has one.
  """
  @spec from_env(atom(), String.t(), String.t()) :: String.t() | nil
  def from_env(config_env, flag_var \\ "UNITARES_GOVERNANCE_ENABLED", url_var \\ "UNITARES_URL") do
    url(config_env, System.get_env(flag_var), System.get_env(url_var))
  end
end
