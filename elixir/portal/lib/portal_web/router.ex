defmodule PortalWeb.Router do
  use PortalWeb, :router

  pipeline :browser do
    plug :accepts, ["html"]
    plug :fetch_session
    plug :fetch_live_flash
    plug :put_root_layout, html: {PortalWeb.Layouts, :root}
    plug :protect_from_forgery
    plug :put_secure_browser_headers
  end

  # Every page but the refusal itself sits behind the operator guard.
  pipeline :operator do
    plug PortalWeb.Plugs.RequireOperator
  end

  pipeline :api do
    plug :accepts, ["json"]
  end

  scope "/", PortalWeb do
    pipe_through :api

    get "/health", HealthController, :index
  end

  scope "/", PortalWeb do
    pipe_through :browser

    get "/signin", SigninController, :show
    get "/signin/start", SigninController, :start
    # Must match the server's UNITARES_HUB_CALLBACK_URL exactly.
    get "/auth/callback", SigninController, :callback
    get "/signout", SigninController, :confirm_signout
    post "/signout", SigninController, :signout
  end

  # The one live_session. Its on_mount guard runs on the page render and
  # again on the socket connect.
  scope "/", PortalWeb do
    pipe_through [:browser, :operator]

    live_session :operator, on_mount: PortalWeb.Live.RequireOperator do
      live "/", TimelineLive
    end
  end
end
