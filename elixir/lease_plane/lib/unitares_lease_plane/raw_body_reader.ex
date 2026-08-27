defmodule UnitaresLeasePlane.RawBodyReader do
  @moduledoc """
  Retains the exact request bytes while `Plug.Parsers` decodes JSON.

  Request-bound identity attestations cover the bytes on the wire, not a
  re-encoded map.  The accumulated body is stored in `conn.private` and never
  logged or returned.
  """

  @private_key :unitares_raw_request_body

  @spec read_body(Plug.Conn.t(), keyword()) ::
          {:ok | :more, binary(), Plug.Conn.t()} | {:error, term()}
  def read_body(conn, opts) do
    case Plug.Conn.read_body(conn, opts) do
      {:ok, bytes, next_conn} -> {:ok, bytes, remember(next_conn, bytes)}
      {:more, bytes, next_conn} -> {:more, bytes, remember(next_conn, bytes)}
      {:error, reason} -> {:error, reason}
    end
  end

  @spec body(Plug.Conn.t()) :: binary()
  def body(conn), do: Map.get(conn.private, @private_key, "")

  defp remember(conn, bytes) do
    previous = Map.get(conn.private, @private_key, "")
    Plug.Conn.put_private(conn, @private_key, previous <> bytes)
  end
end
