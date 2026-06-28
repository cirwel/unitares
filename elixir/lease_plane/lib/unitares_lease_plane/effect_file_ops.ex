defmodule UnitaresLeasePlane.EffectFileOps do
  @moduledoc """
  Filesystem operations for the `file_write` executor, behind a seam so the live
  commit AND every step of the in-process compensation (rollback) can be
  fault-injected in tests — the hard precondition the slice-2 dialectic review
  set for the live-write path. The default delegates to `File`; tests swap in a
  fake via `Application.put_env(:lease_plane, :effect_file_ops, FakeFileOps)`.
  """

  @callback read(path :: String.t()) :: {:ok, binary()} | {:error, term()}
  @callback write(path :: String.t(), bytes :: binary()) :: :ok | {:error, term()}
  @callback rm(path :: String.t()) :: :ok | {:error, term()}

  @behaviour __MODULE__

  @impl true
  def read(path), do: File.read(path)

  @impl true
  def write(path, bytes), do: File.write(path, bytes)

  @impl true
  def rm(path), do: File.rm(path)
end
