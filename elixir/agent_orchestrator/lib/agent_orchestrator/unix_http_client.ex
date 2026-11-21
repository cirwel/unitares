defmodule AgentOrchestrator.UnixHTTPClient do
  @moduledoc """
  Minimal stdlib HTTP/1.1 client over AF_UNIX stream sockets using `:gen_tcp`.

  OTP's built-in `:httpc` does not support Unix domain sockets, and
  `agent_orchestrator` avoids adding a third-party HTTP dependency. This module
  provides a bounded, fail-closed HTTP/1.1 request/response round-trip over
  `{:local, path}` with socket-path safety validation (`lstat` non-symlink,
  `S_ISSOCK`, current-UID ownership, owner-only permissions) and strict framing
  handling for `Content-Length`, `Transfer-Encoding: chunked`, and EOF bodies.
  """

  import Bitwise

  @max_socket_path_bytes 103
  @max_header_bytes 32_768
  @max_body_bytes 1_048_576
  @default_connect_timeout_ms 2_000
  @default_timeout_ms 5_000
  @s_ifmt 0o170000
  @s_ifsock 0o140000
  @uid_cache_key {__MODULE__, :current_uid}

  @valid_header_name ~r/\A[A-Za-z0-9!#$%&'*+\-.^_`|~]+\z/
  @valid_hex ~r/\A[0-9a-fA-F]+\z/
  @valid_digits ~r/\A[0-9]+\z/

  @type header :: {String.t(), String.t()}

  @doc """
  Send an HTTP/1.1 POST request over a Unix domain socket.

  Returns `{:ok, status, headers, body}` or `{:error, reason}`.
  """
  @spec post(String.t(), String.t(), [header()], binary(), keyword()) ::
          {:ok, pos_integer(), %{optional(String.t()) => [String.t()]}, binary()}
          | {:error, term()}
  def post(socket_path, request_path, headers, body, opts \\ []) do
    request("POST", socket_path, request_path, headers, body, opts)
  end

  @doc """
  Send a single HTTP/1.1 request over a Unix domain socket (`Connection: close`).
  """
  @spec request(String.t(), String.t(), String.t(), [header()], binary(), keyword()) ::
          {:ok, pos_integer(), %{optional(String.t()) => [String.t()]}, binary()}
          | {:error, term()}
  def request(method, socket_path, request_path, headers, body, opts \\ [])
      when is_binary(body) do
    connect_timeout_ms = Keyword.get(opts, :connect_timeout_ms, @default_connect_timeout_ms)
    timeout_ms = Keyword.get(opts, :timeout_ms, @default_timeout_ms)
    max_body_bytes = Keyword.get(opts, :max_body_bytes, @max_body_bytes)

    with :ok <- validate_method(method),
         :ok <- validate_request_path(request_path),
         :ok <- validate_headers(headers),
         :ok <- validate_socket_path(socket_path, opts),
         {:ok, sock} <-
           :gen_tcp.connect(
             {:local, socket_path},
             0,
             [:binary, {:active, false}, {:packet, :raw}],
             connect_timeout_ms
           ) do
      deadline = System.monotonic_time(:millisecond) + timeout_ms

      try do
        wire_request = build_request(method, request_path, headers, body)

        with :ok <- :gen_tcp.send(sock, wire_request),
             {:ok, status, resp_headers, rest} <- read_response_head(sock, deadline),
             {:ok, resp_body} <-
               read_response_body(
                 method,
                 status,
                 resp_headers,
                 sock,
                 rest,
                 deadline,
                 max_body_bytes
               ) do
          {:ok, status, resp_headers, resp_body}
        end
      after
        :gen_tcp.close(sock)
      end
    end
  end

  @doc """
  Validate that `path` is a safe, absolute, owner-only Unix domain socket path.

  Checks `File.lstat/1` without following symlinks and enforces:
  - non-empty absolute path without NUL, CR, LF, or `..` segments
  - length <= 103 bytes (POSIX `sockaddr_un.sun_path` safety across macOS/Linux)
  - not a symbolic link
  - POSIX `S_ISSOCK` file type
  - owned by the current process UID
  - no group or world permission bits (`(mode &&& 0o077) == 0`)
  """
  @spec validate_socket_path(term(), keyword()) :: :ok | {:error, term()}
  def validate_socket_path(path, opts \\ [])

  def validate_socket_path(path, opts) when is_binary(path) do
    cond do
      path == "" ->
        {:error, :empty_socket_path}

      String.contains?(path, [<<0>>, "\r", "\n"]) ->
        {:error, :invalid_socket_path}

      Path.type(path) != :absolute ->
        {:error, :relative_socket_path}

      Enum.member?(Path.split(path), "..") ->
        {:error, :path_traversal_not_allowed}

      byte_size(path) > @max_socket_path_bytes ->
        {:error, :socket_path_too_long}

      true ->
        validate_socket_stat(path, opts)
    end
  end

  def validate_socket_path(_other, _opts), do: {:error, :invalid_socket_path}

  defp validate_socket_stat(path, opts) do
    case File.lstat(path) do
      {:ok, %File.Stat{type: :symlink}} ->
        {:error, :symlink_not_allowed}

      {:ok, %File.Stat{type: :other, mode: mode, uid: uid}}
      when (mode &&& @s_ifmt) == @s_ifsock ->
        with :ok <- check_socket_permissions(mode),
             :ok <- check_socket_owner(uid, opts) do
          :ok
        end

      {:ok, %File.Stat{}} ->
        {:error, :not_a_socket}

      {:error, reason} ->
        {:error, {:socket_stat_failed, reason}}
    end
  end

  defp check_socket_permissions(mode) do
    if (mode &&& 0o077) == 0 do
      :ok
    else
      {:error, :insecure_socket_permissions}
    end
  end

  defp check_socket_owner(uid, opts) do
    expected_uid =
      case Keyword.fetch(opts, :expected_uid) do
        {:ok, value} -> value
        :error -> current_uid()
      end

    case expected_uid do
      expected when is_integer(expected) and uid == expected -> :ok
      expected when is_integer(expected) -> {:error, :wrong_socket_owner}
      _ -> {:error, :uid_unavailable}
    end
  end

  @doc false
  def current_uid do
    case :persistent_term.get(@uid_cache_key, :uncached) do
      uid when is_integer(uid) ->
        uid

      :uncached ->
        case System.cmd("id", ["-u"], stderr_to_stdout: true) do
          {out, 0} ->
            case Integer.parse(String.trim(out)) do
              {uid, ""} when uid >= 0 ->
                :persistent_term.put(@uid_cache_key, uid)
                uid

              _ ->
                :error
            end

          _ ->
            :error
        end
    end
  rescue
    _ -> :error
  end

  # ---------- request validation & serialization ----------

  defp validate_method(method) when method in ["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD"],
    do: :ok

  defp validate_method(_other), do: {:error, :invalid_method}

  defp validate_request_path(path) when is_binary(path) do
    if String.starts_with?(path, "/") and not String.match?(path, ~r/[\s\r\n\0]/) do
      :ok
    else
      {:error, :invalid_request_path}
    end
  end

  defp validate_request_path(_other), do: {:error, :invalid_request_path}

  defp validate_headers(headers) when is_list(headers) do
    Enum.reduce_while(headers, :ok, fn
      {name, value}, :ok when is_binary(name) and is_binary(value) ->
        if Regex.match?(@valid_header_name, name) and
             not String.contains?(value, ["\r", "\n", <<0>>]) do
          {:cont, :ok}
        else
          {:halt, {:error, :invalid_header}}
        end

      _other, _acc ->
        {:halt, {:error, :invalid_header}}
    end)
  end

  defp validate_headers(_other), do: {:error, :invalid_header}

  defp build_request(method, request_path, headers, body) do
    filtered_headers =
      Enum.reject(headers, fn {name, _value} ->
        String.downcase(name) in ["host", "connection", "content-length", "transfer-encoding"]
      end)

    wire_headers =
      [
        {"Host", "localhost"},
        {"Connection", "close"},
        {"Content-Length", Integer.to_string(byte_size(body))}
      ] ++ filtered_headers

    header_iodata = Enum.map(wire_headers, fn {name, value} -> [name, ": ", value, "\r\n"] end)

    [method, " ", request_path, " HTTP/1.1\r\n", header_iodata, "\r\n", body]
  end

  # ---------- response parsing ----------

  defp read_response_head(sock, deadline), do: read_response_head(sock, deadline, <<>>)

  defp read_response_head(sock, deadline, acc) do
    case :binary.match(acc, "\r\n\r\n") do
      {pos, 4} when pos <= @max_header_bytes ->
        head = binary_part(acc, 0, pos)
        rest = binary_part(acc, pos + 4, byte_size(acc) - pos - 4)

        with {:ok, status, headers} <- parse_response_head(head) do
          {:ok, status, headers, rest}
        end

      {pos, 4} when pos > @max_header_bytes ->
        {:error, :headers_too_large}

      :nomatch ->
        if byte_size(acc) >= @max_header_bytes do
          {:error, :headers_too_large}
        else
          case recv_chunk(sock, deadline) do
            {:ok, chunk} ->
              next = acc <> chunk

              if byte_size(next) > @max_header_bytes and
                   :binary.match(next, "\r\n\r\n") == :nomatch do
                {:error, :headers_too_large}
              else
                read_response_head(sock, deadline, next)
              end

            {:error, :closed} ->
              {:error, :unexpected_eof}

            {:error, reason} ->
              {:error, reason}
          end
        end
    end
  end

  defp parse_response_head(head) do
    case String.split(head, "\r\n") do
      [status_line | header_lines] ->
        with {:ok, status} <- parse_status_line(status_line),
             {:ok, headers} <- parse_header_lines(header_lines) do
          {:ok, status, headers}
        end

      _ ->
        {:error, :malformed_response}
    end
  end

  defp parse_status_line(line) do
    case Regex.run(~r/\AHTTP\/1\.[01] ([1-5][0-9]{2})(?: .*)?\z/, line) do
      [_, code_str] -> {:ok, String.to_integer(code_str)}
      _ -> {:error, :malformed_status_line}
    end
  end

  defp parse_header_lines(lines) do
    Enum.reduce_while(lines, {:ok, %{}}, fn line, {:ok, acc} ->
      if String.starts_with?(line, [" ", "\t"]) do
        {:halt, {:error, :malformed_header}}
      else
        case String.split(line, ":", parts: 2) do
          [raw_name, raw_value] ->
            name = String.downcase(raw_name)
            value = String.trim(raw_value)

            if name == "" or not Regex.match?(@valid_header_name, name) do
              {:halt, {:error, :malformed_header}}
            else
              updated = Map.update(acc, name, [value], fn existing -> existing ++ [value] end)
              {:cont, {:ok, updated}}
            end

          _ ->
            {:halt, {:error, :malformed_header}}
        end
      end
    end)
  end

  defp read_response_body("HEAD", _status, _headers, _sock, _rest, _deadline, _max_body_bytes),
    do: {:ok, <<>>}

  defp read_response_body(_method, status, _headers, _sock, _rest, _deadline, _max_body_bytes)
       when status in [204, 304] or status in 100..199,
       do: {:ok, <<>>}

  defp read_response_body(_method, _status, headers, sock, rest, deadline, max_body_bytes) do
    te_values = Map.get(headers, "transfer-encoding", [])
    cl_values = Map.get(headers, "content-length", [])

    cond do
      te_values != [] and cl_values != [] ->
        {:error, :conflicting_framing_headers}

      te_values != [] ->
        codings =
          te_values
          |> Enum.flat_map(&String.split(&1, ",", trim: true))
          |> Enum.map(&(&1 |> String.trim() |> String.downcase()))

        if codings == ["chunked"] do
          read_chunked_body(sock, rest, deadline, max_body_bytes, [], 0)
        else
          {:error, :unsupported_transfer_encoding}
        end

      cl_values != [] ->
        with {:ok, length} <- parse_content_length(cl_values) do
          if length > max_body_bytes do
            {:error, :response_too_large}
          else
            read_exact_body(sock, rest, length, deadline)
          end
        end

      true ->
        read_until_eof(sock, rest, deadline, max_body_bytes)
    end
  end

  defp parse_content_length(values) do
    parsed =
      values
      |> Enum.flat_map(&String.split(&1, ",", trim: true))
      |> Enum.map(&String.trim/1)

    with true <- parsed != [],
         true <- Enum.all?(parsed, &Regex.match?(@valid_digits, &1)),
         lengths = Enum.map(parsed, &String.to_integer/1),
         [unique] <- Enum.uniq(lengths) do
      {:ok, unique}
    else
      _ -> {:error, :invalid_content_length}
    end
  end

  defp read_exact_body(_sock, buf, length, _deadline) when byte_size(buf) >= length do
    {:ok, binary_part(buf, 0, length)}
  end

  defp read_exact_body(sock, buf, length, deadline) do
    case recv_chunk(sock, deadline) do
      {:ok, chunk} ->
        read_exact_body(sock, buf <> chunk, length, deadline)

      {:error, :closed} ->
        {:error, :truncated_body}

      {:error, reason} ->
        {:error, reason}
    end
  end

  defp read_until_eof(sock, buf, deadline, max_body_bytes) do
    if byte_size(buf) > max_body_bytes do
      {:error, :response_too_large}
    else
      case recv_chunk(sock, deadline) do
        {:ok, chunk} ->
          read_until_eof(sock, buf <> chunk, deadline, max_body_bytes)

        {:error, :closed} ->
          {:ok, buf}

        {:error, reason} ->
          {:error, reason}
      end
    end
  end

  defp read_chunked_body(sock, buf, deadline, max_body_bytes, chunks, total_bytes) do
    with {:ok, line, rest} <- read_crlf_line(sock, buf, deadline, @max_header_bytes),
         {:ok, size} <- parse_chunk_size(line) do
      cond do
        size == 0 ->
          with :ok <- consume_chunk_trailers(sock, rest, deadline, 0) do
            {:ok, chunks |> Enum.reverse() |> IO.iodata_to_binary()}
          end

        total_bytes + size > max_body_bytes ->
          {:error, :response_too_large}

        true ->
          with {:ok, chunk_with_crlf, next_buf} <- read_at_least(sock, rest, size + 2, deadline),
               <<chunk::binary-size(^size), "\r\n">> <- chunk_with_crlf do
            read_chunked_body(
              sock,
              next_buf,
              deadline,
              max_body_bytes,
              [chunk | chunks],
              total_bytes + size
            )
          else
            {:error, reason} -> {:error, reason}
            _ -> {:error, :malformed_chunk}
          end
      end
    end
  end

  defp consume_chunk_trailers(sock, buf, deadline, consumed_bytes) do
    if consumed_bytes > @max_header_bytes do
      {:error, :headers_too_large}
    else
      with {:ok, line, rest} <- read_crlf_line(sock, buf, deadline, @max_header_bytes) do
        if line == "" do
          :ok
        else
          consume_chunk_trailers(sock, rest, deadline, consumed_bytes + byte_size(line) + 2)
        end
      end
    end
  end

  defp read_crlf_line(sock, buf, deadline, max_line_bytes) do
    case :binary.match(buf, "\r\n") do
      {pos, 2} when pos <= max_line_bytes ->
        line = binary_part(buf, 0, pos)
        rest = binary_part(buf, pos + 2, byte_size(buf) - pos - 2)
        {:ok, line, rest}

      {pos, 2} when pos > max_line_bytes ->
        {:error, :line_too_long}

      :nomatch ->
        if byte_size(buf) > max_line_bytes do
          {:error, :line_too_long}
        else
          case recv_chunk(sock, deadline) do
            {:ok, chunk} -> read_crlf_line(sock, buf <> chunk, deadline, max_line_bytes)
            {:error, :closed} -> {:error, :truncated_body}
            {:error, reason} -> {:error, reason}
          end
        end
    end
  end

  defp read_at_least(_sock, buf, needed, _deadline) when byte_size(buf) >= needed do
    head = binary_part(buf, 0, needed)
    rest = binary_part(buf, needed, byte_size(buf) - needed)
    {:ok, head, rest}
  end

  defp read_at_least(sock, buf, needed, deadline) do
    case recv_chunk(sock, deadline) do
      {:ok, chunk} -> read_at_least(sock, buf <> chunk, needed, deadline)
      {:error, :closed} -> {:error, :truncated_body}
      {:error, reason} -> {:error, reason}
    end
  end

  defp parse_chunk_size(line) do
    size_hex =
      line
      |> String.split(";", parts: 2)
      |> List.first()
      |> String.trim()

    if byte_size(size_hex) in 1..16 and Regex.match?(@valid_hex, size_hex) do
      {:ok, String.to_integer(size_hex, 16)}
    else
      {:error, :malformed_chunk_size}
    end
  end

  defp recv_chunk(sock, deadline) do
    remaining = deadline - System.monotonic_time(:millisecond)

    if remaining <= 0 do
      {:error, :timeout}
    else
      :gen_tcp.recv(sock, 0, remaining)
    end
  end
end
