defmodule AgentOrchestrator.SafeParsers do
  @moduledoc """
  Wraps `Plug.Parsers` so a `Plug.Parsers.ParseError` (malformed JSON) or
  `Plug.Parsers.UnsupportedMediaTypeError` becomes a typed-absence
  `schema_invalid` (422) / `unsupported_media_type` (415) response instead of
  Bandit's default empty-body 400.

  Mirrors `UnitaresLeasePlane.SafeParsers`: relying on `Plug.ErrorHandler` to
  convert these is unreliable (the exceptions carry a `Plug.Exception`
  `plug_status` that the adapter renders directly), so the conversion lives at
  one named site in the pipeline. `Plug.ErrorHandler` stays in the router as the
  backstop for errors raised inside route handlers, not for parse errors.
  """

  @behaviour Plug

  alias Plug.Conn
  alias Plug.Parsers

  @impl true
  def init(opts), do: Parsers.init(opts)

  @impl true
  def call(conn, opts) do
    try do
      Parsers.call(conn, opts)
    rescue
      e in Parsers.ParseError ->
        send_typed(conn, 422, "schema_invalid", "malformed request body: #{Exception.message(e)}")

      e in Parsers.UnsupportedMediaTypeError ->
        send_typed(conn, 415, "unsupported_media_type", "unsupported media type: #{Exception.message(e)}")
    end
  end

  defp send_typed(conn, status, error, detail) do
    body = Jason.encode!(%{ok: false, error: error, detail: detail})

    conn
    |> Conn.put_resp_content_type("application/json")
    |> Conn.send_resp(status, body)
    |> Conn.halt()
  end
end
