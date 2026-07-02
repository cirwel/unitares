defmodule UnitaresLeasePlane.CanonicalPayload do
  @moduledoc """
  Canonical payload serialization for effect-binding (#1075 / #1252).

  An effect grant binds `payload_sha256` — a hash the proposer computes at
  mint time (Python, `unitares_sdk.lease_plane.canonical`) and this plane
  recomputes over the parsed payload when it forwards the grant to
  `/v1/effect-veto`. Both sides must serialize the payload byte-identically
  before hashing; a mismatch fails CLOSED at the veto. The shared fixture
  `tests/vectors/effect_payload_canonical.json` pins both implementations.

  Canonical form:

  - JSON, UTF-8, compact (no whitespace), object keys sorted bytewise
    (binary term order on UTF-8 binaries equals codepoint order, matching
    Python's `sort_keys`).
  - Non-ASCII emitted raw (Jason's default), including non-BMP.
  - Object keys must be strings. Values: string, integer, boolean, nil,
    object, array.
  - **Floats are rejected** (`{:error, :float_in_payload}`): float
    formatting is not stable across languages.
  - **Control characters in strings/keys are rejected EXCEPT the five
    short-escape characters** `\\b \\t \\n \\f \\r` (U+0008, U+0009, U+000A,
    U+000C, U+000D) — both encoders spell those five identically (pinned
    by the shared fixture), and real text content contains them. The
    remaining C0 controls (U+0000–U+0007, U+000B, U+000E–U+001F) are the
    genuinely divergent region (Jason emits uppercase hex escapes where
    Python emits lowercase) and stay rejected
    (`{:error, :control_char_in_payload}`).
  """

  @type reason ::
          :not_a_map
          | :float_in_payload
          | :control_char_in_payload
          | :non_string_key
          | :unsupported_type

  @doc "Canonical UTF-8 bytes of `payload`, or a fail-closed error."
  @spec bytes(term()) :: {:ok, binary()} | {:error, reason()}
  def bytes(payload) when is_map(payload) do
    case canonicalize(payload) do
      {:ok, canonical} -> {:ok, Jason.encode!(canonical)}
      {:error, _} = err -> err
    end
  end

  def bytes(_), do: {:error, :not_a_map}

  @doc "Lowercase-hex SHA-256 of the canonical payload bytes."
  @spec sha256(term()) :: {:ok, String.t()} | {:error, reason()}
  def sha256(payload) do
    with {:ok, bin} <- bytes(payload) do
      {:ok, :crypto.hash(:sha256, bin) |> Base.encode16(case: :lower)}
    end
  end

  # ---- recursive validate + sorted transform ----

  defp canonicalize(map) when is_map(map) do
    map
    |> Enum.sort_by(fn {k, _v} -> k end)
    |> Enum.reduce_while({:ok, []}, fn {k, v}, {:ok, acc} ->
      with :ok <- check_key(k),
           {:ok, cv} <- canonicalize(v) do
        {:cont, {:ok, [{k, cv} | acc]}}
      else
        {:error, _} = err -> {:halt, err}
      end
    end)
    |> case do
      {:ok, pairs} -> {:ok, %Jason.OrderedObject{values: Enum.reverse(pairs)}}
      {:error, _} = err -> err
    end
  end

  defp canonicalize(list) when is_list(list) do
    list
    |> Enum.reduce_while({:ok, []}, fn item, {:ok, acc} ->
      case canonicalize(item) do
        {:ok, ci} -> {:cont, {:ok, [ci | acc]}}
        {:error, _} = err -> {:halt, err}
      end
    end)
    |> case do
      {:ok, items} -> {:ok, Enum.reverse(items)}
      {:error, _} = err -> err
    end
  end

  defp canonicalize(f) when is_float(f), do: {:error, :float_in_payload}
  defp canonicalize(b) when is_boolean(b), do: {:ok, b}
  defp canonicalize(i) when is_integer(i), do: {:ok, i}
  defp canonicalize(nil), do: {:ok, nil}

  defp canonicalize(s) when is_binary(s) do
    if has_control_char?(s), do: {:error, :control_char_in_payload}, else: {:ok, s}
  end

  defp canonicalize(_), do: {:error, :unsupported_type}

  defp check_key(k) when is_binary(k) do
    if has_control_char?(k), do: {:error, :control_char_in_payload}, else: :ok
  end

  defp check_key(_), do: {:error, :non_string_key}

  # UTF-8 continuation bytes are always >= 0x80, so a raw byte scan for
  # < 0x20 exactly detects control codepoints without decoding. The five
  # short-escape controls \b \t \n \f \r (0x08 0x09 0x0A 0x0C 0x0D) are
  # admitted: Jason and Python's json.dumps emit them byte-identically.
  # Every other C0 control diverges (Jason emits uppercase hex escapes,
  # "\\u000B", where Python emits lowercase "\\u000b") and stays rejected.
  @short_escape_ok [0x08, 0x09, 0x0A, 0x0C, 0x0D]

  defp has_control_char?(<<b, rest::binary>>) when b < 0x20 do
    if b in @short_escape_ok, do: has_control_char?(rest), else: true
  end

  defp has_control_char?(<<_b, rest::binary>>), do: has_control_char?(rest)
  defp has_control_char?(<<>>), do: false
end
