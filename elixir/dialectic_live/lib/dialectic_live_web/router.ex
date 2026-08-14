defmodule DialecticLiveWeb.Router do
  use DialecticLiveWeb, :router

  pipeline :browser do
    plug :accepts, ["html"]
    plug :fetch_session
    plug :fetch_live_flash
    plug :put_root_layout, html: {DialecticLiveWeb.Layouts, :root}
    plug :protect_from_forgery
    plug :put_secure_browser_headers
  end

  pipeline :api do
    plug :accepts, ["json"]
  end

  # Before the browser scope: /health is machine-facing and must not pick up
  # session/CSRF/layout plugs it has no use for.
  scope "/", DialecticLiveWeb do
    pipe_through :api

    get "/health", HealthController, :index
  end

  scope "/", DialecticLiveWeb do
    pipe_through :browser

    live "/", DialecticLive
    live "/dialectic", DialecticLive
  end

  # Other scopes may use custom stacks.
  # scope "/api", DialecticLiveWeb do
  #   pipe_through :api
  # end

  # Enable LiveDashboard in development
  if Application.compile_env(:dialectic_live, :dev_routes) do
    # If you want to use the LiveDashboard in production, you should put
    # it behind authentication and allow only admins to access it.
    # If your application does not have an admins-only section yet,
    # you can use Plug.BasicAuth to set up some basic authentication
    # as long as you are also using SSL (which you should anyway).
    import Phoenix.LiveDashboard.Router

    scope "/dev" do
      pipe_through :browser

      live_dashboard "/dashboard", metrics: DialecticLiveWeb.Telemetry
    end
  end
end
