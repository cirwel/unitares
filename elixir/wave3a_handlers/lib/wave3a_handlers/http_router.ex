defmodule Wave3aHandlers.HTTPRouter do
  @moduledoc """
  HTTP surface for the Wave 3a BEAM handler app.

  RFC `docs/proposals/beam-wave-3a-read-only-handlers.md` v0.2 §2.2 pins the
  envelope shape; §5 PR #4 scopes this PR's routes; §6 Q3 marks the
  separate-app-vs-merge-into-3b decision as deferred to Wave 3b.

  ## Routes

    * `GET /health` — auth-exempt liveness probe. Returns
      `{"ok": true, "protocol_version": "wave3a.v1"}`. PR #1's Python probe
      uses the analogous shape at `/v1/probe/health`. This route MUST work
      even when the bearer token is rotating; the auth plug exempts it.

    * `POST /v1/handlers/:tool_name` — bearer-gated. PR #4 ships an empty
      dispatch table; every tool name returns 501 with envelope
      `{"ok": false, "protocol_version": "wave3a.v1", "error":
      "not_implemented", "reason": "handler not wired"}`. PR #5 cuts over
      the first real handler (`health_check`).

  ## Protocol version

  Pinned to the literal `"wave3a.v1"`. This is INTENTIONALLY distinct from
  the lease plane's `"v1.0"` — council finding FIND-V5/V7 (PR #539
  verifier lane) discovered the mismatch and the v0.2 RFC §2.2 resolves it
  by committing the Wave 3a surface to `"wave3a.v1"`. The Python proxy at
  `src/wave3a_beam_proxy.py:79` pins the same string; both sides have
  ExUnit/pytest tests that fail if the literal drifts.

  ## Envelope shape

  Every response — success, 4xx, 5xx — is a JSON object with top-level
  keys (never nested under a `data` envelope). The §2.2 shapes are:

      success:   {"ok": true,  "protocol_version": "wave3a.v1", ...}
      401:       {"ok": false, "protocol_version": "wave3a.v1",
                  "error": "permission_denied",
                  "reason": "bearer token missing or invalid"}
      503 (token unset): {"ok": false, "protocol_version": "wave3a.v1",
                  "error": "service_unavailable",
                  "reason": "WAVE_3A_BEAM_TOKEN not configured"}
      501 (no handler wired): {"ok": false, "protocol_version": "wave3a.v1",
                  "error": "not_implemented",
                  "reason": "handler not wired"}

  The Python proxy's `_validate_success_envelope` (PR #3) checks
  `body["ok"] is True` and `body["protocol_version"] == "wave3a.v1"` —
  every success envelope here MUST pass that, byte-for-byte.
  """

  use Plug.Router
  use Plug.ErrorHandler

  require Logger

  plug(:match)

  # Auth runs before body parsing — same reason as lease plane: an
  # unauthenticated caller can't probe endpoint existence by sending
  # malformed JSON, and bearer validation only needs the header.
  plug(Wave3aHandlers.HTTPAuth)

  plug(Plug.Parsers,
    parsers: [:json],
    pass: ["application/json"],
    json_decoder: Jason
  )

  plug(:dispatch)

  # Plug.ErrorHandler catches anything raised inside route handlers and
  # converts it to a typed 503 with a redacted reason — matches the lease
  # plane's posture so a leaked inspect-string never reaches the wire.
  @impl Plug.ErrorHandler
  def handle_errors(conn, %{kind: kind, reason: reason, stack: _stack}) do
    Logger.error(
      "wave3a_handlers HTTP error: kind=#{kind} reason=#{Exception.format_banner(kind, reason)}"
    )

    json(conn, 503, %{
      ok: false,
      error: "service_unavailable",
      reason: "internal error"
    })
  end

  # ---------- /health ----------
  # Auth-exempt liveness probe (HTTPAuth's auth_exempt?/1 lets this through
  # without checking the bearer). Shape matches PR #1's `/v1/probe/health`
  # so a future supervisor-side probe can hit either side identically.
  get "/health" do
    json(conn, 200, %{ok: true})
  end

  # ---------- /v1/handlers/:tool_name ----------
  # PR #4 ships an empty dispatch table. Every tool name returns 501. PR #5
  # cuts over `health_check` and the dispatch shape lands then.
  post "/v1/handlers/:tool_name" do
    json(conn, 501, %{
      ok: false,
      error: "not_implemented",
      reason: "handler not wired",
      tool_name: tool_name
    })
  end

  # ---------- catch-all ----------
  match _ do
    json(conn, 404, %{ok: false, error: "not_found"})
  end

  # ---------- helpers ----------

  # Per FIND-V5/V7 council finding: Wave 3a's contract is independent of
  # the lease plane's "v1.0". This module-level constant is the load-bearing
  # literal; `protocol_version_test.exs` pins it.
  @protocol_version "wave3a.v1"

  @doc """
  Boundary protocol version. Public so the ExUnit test suite can pin the
  constant; Python-side callers receive it in every response body as the
  `protocol_version` field, validated by
  `src/wave3a_beam_proxy.py::_validate_success_envelope`.
  """
  @spec protocol_version() :: String.t()
  def protocol_version, do: @protocol_version

  defp json(conn, status, body) do
    versioned_body = Map.put(body, :protocol_version, @protocol_version)

    conn
    |> Plug.Conn.put_resp_content_type("application/json")
    |> Plug.Conn.send_resp(status, Jason.encode!(versioned_body))
  end
end
