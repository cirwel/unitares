defmodule UnitaresSdk.Transport do
  @moduledoc """
  HTTP transport for the governance tool bridge.

  The bridge is `POST {base_url}/v1/tools/call` with a `{"name", "arguments"}`
  body — the same surface any non-MCP client uses, with no MCP handshake. Four
  BEAM clients each built their own `:httpc` call for it; this is that call,
  once.

  Everything here is deliberately `:httpc` + Erlang `:json` so the SDK carries
  no third-party runtime dependency (see the repo execution-cost policy). A
  consumer that already runs Finch or Req can inject its own poster via
  `:http_post` rather than adopt ours.
  """

  alias UnitaresSdk.Envelope

  @tool_path "/v1/tools/call"

  # An emit is cheap; an onboard mints an identity and initialises EISV. A
  # single 4s onboard timeout once left a harness governance feed dark for
  # 9.5h, so the two are NOT given the same budget.
  @default_timeout 4_000
  @onboard_timeout 15_000

  @type opts :: [timeout: pos_integer(), http_post: (String.t(), iodata(), keyword() -> term())]

  @doc "Default timeout for an ordinary tool call, in milliseconds."
  @spec default_timeout() :: pos_integer()
  def default_timeout, do: @default_timeout

  @doc """
  Timeout for identity-minting calls. Use this for `onboard`, never the
  ordinary default — see the 9.5h note above.
  """
  @spec onboard_timeout() :: pos_integer()
  def onboard_timeout, do: @onboard_timeout

  @doc """
  Call a governance tool.

  `base_url` is the substrate root (`http://localhost:8767`), not the tool
  path — a trailing slash is tolerated. Returns whatever `Envelope.decode/1`
  makes of the response, so a strict refusal surfaces as `{:error, {:refused,
  _}}` rather than a clean-looking `{:ok, _}`.

  Never raises: a dead or hung endpoint must not be able to crash a caller's
  GenServer.
  """
  @spec call_tool(String.t(), String.t(), map(), opts()) :: Envelope.result()
  def call_tool(base_url, name, args, opts \\ [])

  def call_tool(nil, _name, _args, _opts), do: {:error, :disabled}

  def call_tool(base_url, name, args, opts) when is_binary(base_url) and is_binary(name) do
    timeout = Keyword.get(opts, :timeout, @default_timeout)
    url = normalize_url(base_url) <> @tool_path

    case encode(%{"name" => name, "arguments" => args}) do
      {:ok, body} ->
        post = Keyword.get(opts, :http_post, &httpc_post/3)

        case post.(url, body, timeout: timeout) do
          {:ok, 200, resp} -> Envelope.decode(resp)
          {:ok, code, _resp} -> {:error, {:http, code}}
          {:error, reason} -> {:error, reason}
          other -> {:error, {:unexpected_transport_result, other}}
        end

      :error ->
        {:error, :unencodable_arguments}
    end
  end

  @doc """
  Strip a trailing slash so `base <> "/v1/tools/call"` never doubles up.
  Returns `nil` for nil/empty, which callers treat as "governance disabled".
  """
  @spec normalize_url(String.t() | nil) :: String.t() | nil
  def normalize_url(nil), do: nil
  def normalize_url(""), do: nil
  def normalize_url(url) when is_binary(url), do: String.trim_trailing(url, "/")

  # --- internals ------------------------------------------------------------

  defp httpc_post(url, body, opts) do
    timeout = Keyword.fetch!(opts, :timeout)
    request = {String.to_charlist(url), [], ~c"application/json", body}

    case :httpc.request(:post, request, [timeout: timeout], body_format: :binary) do
      {:ok, {{_, code, _}, _headers, resp}} -> {:ok, code, resp}
      {:error, reason} -> {:error, reason}
    end
  rescue
    e -> {:error, e}
  catch
    :exit, reason -> {:error, {:exit, reason}}
  end

  defp encode(term) do
    {:ok, IO.iodata_to_binary(:json.encode(term))}
  rescue
    _ -> :error
  catch
    _, _ -> :error
  end
end
