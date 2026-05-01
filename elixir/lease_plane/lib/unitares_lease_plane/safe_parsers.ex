defmodule UnitaresLeasePlane.SafeParsers do
  @moduledoc """
  Wraps `Plug.Parsers` so a `Plug.Parsers.ParseError` (malformed JSON,
  unsupported media type) becomes a typed-absence `schema_invalid` 422
  instead of Bandit's default empty-body 400.

  Why a shim instead of moving Plug.Parsers behind a try/rescue inline:
  the router pipeline runs plugs in declaration order, and we want one
  named site for the conversion so future contributors can find it.
  """

  @behaviour Plug

  alias Plug.Parsers
  alias Plug.Conn

  @impl true
  def init(opts), do: Parsers.init(opts)

  @impl true
  def call(conn, opts) do
    try do
      Parsers.call(conn, opts)
    rescue
      e in Parsers.ParseError ->
        body =
          Jason.encode!(%{
            ok: false,
            error: "schema_invalid",
            detail: "malformed request body: #{Exception.message(e)}"
          })

        conn
        |> Conn.put_resp_content_type("application/json")
        |> Conn.send_resp(422, body)
        |> Conn.halt()

      e in Parsers.UnsupportedMediaTypeError ->
        body =
          Jason.encode!(%{
            ok: false,
            error: "schema_invalid",
            detail: "unsupported media type: #{Exception.message(e)}"
          })

        conn
        |> Conn.put_resp_content_type("application/json")
        |> Conn.send_resp(415, body)
        |> Conn.halt()
    end
  end
end
