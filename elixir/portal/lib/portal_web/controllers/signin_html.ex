defmodule PortalWeb.SigninHTML do
  @moduledoc "The page every refused request gets."

  @doc "The sign-in-required page, as a complete HTML document."
  def required do
    """
    <!doctype html>
    <html lang="en"><head><meta charset="utf-8"><title>Sign in · UNITARES hub</title></head>
    <body><main>
    <h1>Sign-in required</h1>
    <p>The hub signs you in through the governance server's passkey login.
    That handoff is not built yet (portal-hub-v0, H2), so nothing here is
    available.</p>
    </main></body></html>
    """
  end
end
