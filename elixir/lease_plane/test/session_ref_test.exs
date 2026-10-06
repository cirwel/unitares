defmodule UnitaresLeasePlane.SessionRefTest do
  use ExUnit.Case, async: true

  alias UnitaresLeasePlane.SessionRef

  test "a keyed session id becomes the digest governance stores" do
    # Fixture from src/mcp_handlers/identity/stable_session.py audit_reference.
    assert SessionRef.reference("agent-5e728ecb-123-xjc4uauir6jvdvqxylny") ==
             "csid:96ac49cb17c64d38ad2468be"
  end

  test "other values pass through, so a digest is not hashed twice" do
    assert SessionRef.reference("csid:96ac49cb17c64d38ad2468be") ==
             "csid:96ac49cb17c64d38ad2468be"

    assert SessionRef.reference("agent-5e728ecb-123") == "agent-5e728ecb-123"
    assert SessionRef.reference("test-session") == "test-session"
    assert SessionRef.reference(nil) == nil
  end
end
