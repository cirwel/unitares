defmodule PortalWeb.SigninHTML do
  @moduledoc "The hub's sign-in pages, as complete HTML documents."

  @doc "What a refused request gets: the sign-in page, or why there is none."
  def refusal, do: if(Portal.Gov.configured?(), do: signin(), else: required())

  @doc "Shown when the handoff is not configured: nothing here is available."
  def required do
    document("""
    <h1>Sign-in is not set up</h1>
    <p>The hub signs you in through the governance server's passkey login, and
    that handoff is not configured on this machine (see the hub's README), so
    nothing here is available.</p>
    """)
  end

  @doc "The sign-in page."
  def signin do
    document("""
    <h1>Sign in</h1>
    <p>The hub signs you in through the governance dashboard's passkey. You
    will be asked to confirm there, then brought back here.</p>
    <p><a href="/signin/start">Sign in with your passkey</a></p>
    """)
  end

  @doc "Shown when a return from the dashboard does not complete a sign-in."
  def failed do
    document("""
    <h1>Sign-in did not complete</h1>
    <p>The approval had expired, was already used, or did not belong to a
    sign-in started in this browser.</p>
    <p><a href="/signin/start">Try again</a></p>
    """)
  end

  @doc """
  The landing page after a sign-in. It moves on to the home screen itself, so
  that navigation starts on the hub's own page and carries its session cookie.
  """
  def signed_in do
    """
    <!doctype html>
    <html lang="en"><head><meta charset="utf-8"><meta name="referrer" content="no-referrer">
    <meta http-equiv="refresh" content="0;url=/"><title>Signed in · UNITARES hub</title></head>
    <body><main>
    <h1>Signed in</h1>
    <p><a href="/">Continue to the hub</a></p>
    </main></body></html>
    """
  end

  @doc "The sign-out confirmation, a form carrying the CSRF token."
  def signout(csrf_token) do
    document("""
    <h1>Sign out of the hub?</h1>
    #{signout_form(csrf_token, "Sign out")}
    <p><a href="/">Cancel</a></p>
    """)
  end

  @doc "Shown when the server could not end the session: it is still live."
  def signout_failed(csrf_token) do
    document("""
    <h1>Sign-out did not complete</h1>
    <p>The governance server could not be reached to end this session, so it is
    still signed in. Try again once the server is back.</p>
    #{signout_form(csrf_token, "Try again")}
    """)
  end

  defp signout_form(csrf_token, label) do
    token = csrf_token |> Phoenix.HTML.html_escape() |> Phoenix.HTML.safe_to_string()

    """
    <form method="post" action="/signout">
    <input type="hidden" name="_csrf_token" value="#{token}">
    <button type="submit">#{label}</button>
    </form>
    """
  end

  defp document(body) do
    """
    <!doctype html>
    <html lang="en"><head><meta charset="utf-8"><meta name="referrer" content="no-referrer"><title>Sign in · UNITARES hub</title></head>
    <body><main>
    #{body}</main></body></html>
    """
  end
end
