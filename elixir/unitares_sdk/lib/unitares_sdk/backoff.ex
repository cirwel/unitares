defmodule UnitaresSdk.Backoff do
  @moduledoc """
  Doubling backoff with a cap, for onboard retry and circuit breaking.

  Two fleet lessons are baked into the defaults:

    * **Onboard must retry, never permanently disable.** A single transient
      onboard failure once disabled a harness's governance feed for the whole
      process lifetime — observed dark for 9.5h off one 4s timeout. Retry base
      5s, cap 60s.

    * **Never initialise a monotonic deadline to 0.** BEAM monotonic time has
      an arbitrary (usually negative) epoch, so `now < 0` is true at boot and a
      breaker initialised to `0` silently skips every call. `nil` means closed;
      there is no zero sentinel in this module for that reason.
  """

  @onboard_base_ms 5_000
  @onboard_cap_ms 60_000

  @breaker_base_ms 15_000
  @breaker_cap_ms 120_000
  @breaker_threshold 2

  @doc "Base delay for onboard retry, in milliseconds."
  @spec onboard_base() :: pos_integer()
  def onboard_base, do: @onboard_base_ms

  @doc "Maximum onboard retry delay, in milliseconds."
  @spec onboard_cap() :: pos_integer()
  def onboard_cap, do: @onboard_cap_ms

  @doc "Consecutive failures that open the breaker."
  @spec breaker_threshold() :: pos_integer()
  def breaker_threshold, do: @breaker_threshold

  @doc """
  Next delay after a failure: double, then clamp to `cap`.

  ## Examples

      iex> UnitaresSdk.Backoff.next(5_000, 60_000)
      10_000

      iex> UnitaresSdk.Backoff.next(45_000, 60_000)
      60_000
  """
  @spec next(pos_integer(), pos_integer()) :: pos_integer()
  def next(current, cap) when is_integer(current) and is_integer(cap) and current > 0,
    do: min(current * 2, cap)

  @doc "Next onboard retry delay. `nil` yields the base delay."
  @spec next_onboard(pos_integer() | nil) :: pos_integer()
  def next_onboard(nil), do: @onboard_base_ms
  def next_onboard(current), do: next(current, @onboard_cap_ms)

  @doc "Next breaker backoff. `nil` yields the base delay."
  @spec next_breaker(pos_integer() | nil) :: pos_integer()
  def next_breaker(nil), do: @breaker_base_ms
  def next_breaker(current), do: next(current, @breaker_cap_ms)

  @doc """
  Whether a breaker deadline has passed.

  `nil` means the breaker is closed — pass the deadline you stored from
  `System.monotonic_time(:millisecond) + delay`, never `0`.
  """
  @spec open?(integer() | nil, integer()) :: boolean()
  def open?(nil, _now), do: false
  def open?(blocked_until_ms, now) when is_integer(blocked_until_ms), do: now < blocked_until_ms
end
