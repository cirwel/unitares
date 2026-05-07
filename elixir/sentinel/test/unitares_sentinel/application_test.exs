defmodule UnitaresSentinel.ApplicationTest do
  use ExUnit.Case, async: false

  alias UnitaresSentinel.Application, as: SentinelApplication

  @agent_uuid "11111111-1111-1111-1111-111111111111"
  @continuity_token "v1.test-token"
  @client_session_id "session-test"

  setup do
    keys = [:emit_checkins, :session_file_path, :legacy_session_file_path]
    previous = Map.new(keys, &{&1, Application.get_env(:unitares_sentinel, &1)})

    on_exit(fn ->
      Enum.each(previous, fn
        {key, nil} -> Application.delete_env(:unitares_sentinel, key)
        {key, value} -> Application.put_env(:unitares_sentinel, key, value)
      end)
    end)

    :ok
  end

  test "fleet finding emitter carries the Sentinel anchor when check-ins are enabled" do
    path =
      Path.join(System.tmp_dir!(), "sentinel-anchor-#{System.unique_integer([:positive])}.json")

    File.write!(
      path,
      Jason.encode!(%{
        "agent_uuid" => @agent_uuid,
        "continuity_token" => @continuity_token,
        "client_session_id" => @client_session_id
      })
    )

    on_exit(fn -> File.rm(path) end)

    Application.put_env(:unitares_sentinel, :emit_checkins, true)
    Application.put_env(:unitares_sentinel, :session_file_path, path)
    Application.delete_env(:unitares_sentinel, :legacy_session_file_path)

    opts = SentinelApplication.fleet_finding_emitter_opts()

    assert opts[:checkin_opts][:anchor] == %{
             "agent_uuid" => @agent_uuid,
             "continuity_token" => @continuity_token,
             "client_session_id" => @client_session_id
           }
  end
end
