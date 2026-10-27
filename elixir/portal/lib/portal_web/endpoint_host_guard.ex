defmodule PortalWeb.EndpointHostGuard do
  @moduledoc """
  Wraps an endpoint's `call/2` in `PortalWeb.Plugs.HostAllowlist`, so a request
  for another Host or port is refused before Phoenix dispatches anything,
  a LiveView socket upgrade included. Register it with `@before_compile`
  after `use Phoenix.Endpoint`, whose own hook defines the `call/2` this wraps.
  """

  defmacro __before_compile__(_env) do
    quote do
      defoverridable call: 2

      def call(conn, opts) do
        case PortalWeb.Plugs.HostAllowlist.call(conn, []) do
          %Plug.Conn{halted: true} = refused -> refused
          allowed -> super(allowed, opts)
        end
      end
    end
  end
end
