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
  - **Control characters (U+0000–U+001F) in strings/keys are rejected**
    (`{:error, :control_char_in_payload}`): the one region where JSON
    escape spelling can differ between encoders; real payloads (paths,
    base64 content) never contain them.
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
  # < 0x20 exactly detects control codepoints without decoding.
  defp has_control_char?(<<b, _rest::binary>>) when b < 0x20, do: true
  defp has_control_char?(<<_b, rest::binary>>), do: has_control_char?(rest)
  defp has_control_char?(<<>>), do: false
end
