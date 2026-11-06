defmodule AgentOrchestrator.AgentRunner.OutputBuffer do
  @moduledoc """
  Bounded output ring buffer, secret redaction, UTF-8 boundary splitting for
  oversized chunks, and execution summary/result formatting for
  `AgentOrchestrator.AgentRunner`.
  """

  # Values shorter than 4 bytes are not redacted: replacing them would mangle
  # ordinary output while hiding almost nothing. Output is redacted line by
  # line, so a value holding a line break could pass in pieces; profiles refuse
  # both kinds of secret, and neither is accepted here.
  @doc false
  def redact_values(values) when is_list(values) do
    values
    |> Enum.filter(
      &(is_binary(&1) and byte_size(&1) >= 4 and not String.contains?(&1, ["\n", "\r"]))
    )
    |> Enum.uniq()
  end

  def redact_values(_), do: []

  # Secrets arrive behind a closure (see the :secret_env spec field) so the
  # spawn request, a GenServer call that times out, or a supervisor report
  # never prints them.
  @doc false
  def secret_env(spec) do
    case Map.get(spec, :secret_env) do
      fun when is_function(fun, 0) -> fun.()
      _ -> []
    end
  end

  @doc false
  def redact_closure(spec) do
    values =
      redact_values(
        List.wrap(Map.get(spec, :redact)) ++ Enum.map(secret_env(spec), fn {_k, v} -> v end)
      )

    fn -> values end
  end

  @doc false
  def redactions(%{redact: fun}) when is_function(fun, 0), do: fun.()
  def redactions(_state), do: []

  @doc false
  def redact_line(line, []), do: line
  def redact_line(line, values), do: String.replace(line, values, "[redacted]")

  # An oversized fragment is published as a synthetic line. With secrets to
  # redact, a secret may straddle the cut, so redact what is complete and hold
  # back the last (longest secret - 1) bytes, which no complete secret can
  # start in, as the start of the next fragment.
  # Either way the cut moves back to a UTF-8 character boundary, so neither
  # fragment holds half a character (Jason refuses invalid UTF-8).
  @doc false
  def flush_oversized(state, partial) do
    {text, hold} =
      case redactions(state) do
        [] ->
          {partial, 0}

        values ->
          {redact_line(partial, values), (values |> Enum.map(&byte_size/1) |> Enum.max()) - 1}
      end

    cut = char_boundary(text, max(byte_size(text) - hold, 0))
    head = binary_part(text, 0, cut)
    tail = binary_part(text, cut, byte_size(text) - cut)
    %{push_line(%{state | partial: ""}, head) | partial: tail}
  end

  # A cut inside a character steps back over its continuation bytes
  # (0b10xxxxxx); a cut at the end holds back a trailing character the chunk
  # boundary left incomplete. Bytes that are not UTF-8 are cut as they are.
  @doc false
  def char_boundary(text, cut) do
    cut = step_back(text, cut, 3)
    if cut == byte_size(text), do: complete_end(text, cut), else: cut
  end

  defp step_back(_text, 0, _left), do: 0
  defp step_back(_text, cut, 0), do: cut

  defp step_back(text, cut, left) do
    if cut < byte_size(text) and continuation?(:binary.at(text, cut)),
      do: step_back(text, cut - 1, left - 1),
      else: cut
  end

  @doc false
  def complete_end(text, size) do
    case Enum.find(max(size - 1, 0)..max(size - 4, 0)//-1, fn i ->
           i < size and not continuation?(:binary.at(text, i))
         end) do
      nil ->
        size

      lead_at ->
        need = sequence_length(:binary.at(text, lead_at))
        if need > 1 and lead_at + need > size, do: lead_at, else: size
    end
  end

  @doc false
  def continuation?(byte), do: Bitwise.band(byte, 0xC0) == 0x80

  @doc false
  def sequence_length(byte) when byte < 0x80, do: 1
  def sequence_length(byte) when Bitwise.band(byte, 0xE0) == 0xC0, do: 2
  def sequence_length(byte) when Bitwise.band(byte, 0xF0) == 0xE0, do: 3
  def sequence_length(byte) when Bitwise.band(byte, 0xF8) == 0xF0, do: 4
  def sequence_length(_byte), do: 1

  @doc false
  def push_line(state, line) do
    # Bounded ring: keep newest max_output_lines. Stored newest-first for O(1)
    # prepend; reversed on read in result/1.
    output = [redact_line(line, redactions(state)) | state.output]

    {output, count} =
      if state.output_count + 1 > state.max_output_lines do
        {Enum.take(output, state.max_output_lines), state.max_output_lines}
      else
        {output, state.output_count + 1}
      end

    %{state | output: output, output_count: count, partial: ""}
  end

  @doc false
  def result(state) do
    state
    |> execution_summary()
    |> Map.merge(%{
      lease_released: state.release_status == :ok,
      output: Enum.reverse(state.output)
    })
  end

  @doc false
  def execution_summary(state) do
    %{
      execution_id: state.execution_id,
      agent_id: state.agent_id,
      cmd: state.cmd,
      started_at: state.started_at,
      os_pid: state.os_pid,
      lease_id: state.lease_id,
      presence: state.presence,
      lineage: state.lineage,
      profile: state.profile,
      exit_status: state.exit_status,
      running: state.exit_status == nil
    }
  end
end
