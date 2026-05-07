defmodule UnitaresSentinel.SessionAnchorTest do
  use ExUnit.Case, async: false

  import ExUnit.CaptureIO

  alias UnitaresSentinel.SessionAnchor

  @agent_uuid "11111111-1111-1111-1111-111111111111"
  @continuity_token "v1.test-token"
  @client_session_id "session-test"

  setup do
    tmpdir =
      System.tmp_dir!()
      |> Path.join("unitares_sentinel_session_anchor_test_#{System.unique_integer([:positive])}")

    File.mkdir_p!(tmpdir)

    on_exit(fn ->
      Mix.Task.clear()
      File.rm_rf!(tmpdir)
    end)

    {:ok,
     tmpdir: tmpdir,
     session: Path.join(tmpdir, "sentinel.json"),
     legacy: Path.join(tmpdir, ".sentinel_session")}
  end

  test "load reads Python anchor fields and preserves unknown metadata", ctx do
    write_json!(ctx.session, %{
      "agent_uuid" => @agent_uuid,
      "continuity_token" => @continuity_token,
      "client_session_id" => @client_session_id,
      "runtime" => "python",
      "extra" => %{"preserve" => true}
    })

    assert {:ok, anchor} = SessionAnchor.load(path: ctx.session, legacy_path: ctx.legacy)

    assert anchor["agent_uuid"] == @agent_uuid
    assert anchor["continuity_token"] == @continuity_token
    assert anchor["client_session_id"] == @client_session_id
    assert anchor["runtime"] == "python"
    assert anchor["extra"] == %{"preserve" => true}

    assert SessionAnchor.resume_payload(anchor) == %{
             "name" => "Sentinel",
             "agent_uuid" => @agent_uuid,
             "continuity_token" => @continuity_token,
             "client_session_id" => @client_session_id,
             "resume" => true
           }
  end

  test "continuity token is optional for Python-compatible UDS anchors", ctx do
    write_json!(ctx.session, %{"agent_uuid" => @agent_uuid})

    assert {:ok, anchor} = SessionAnchor.load(path: ctx.session, legacy_path: ctx.legacy)

    assert SessionAnchor.resume_payload(anchor) == %{
             "name" => "Sentinel",
             "agent_uuid" => @agent_uuid,
             "resume" => true
           }
  end

  test "load rejects anchors without agent_uuid", ctx do
    write_json!(ctx.session, %{"continuity_token" => @continuity_token})

    assert {:error, :missing_agent_uuid} =
             SessionAnchor.load(path: ctx.session, legacy_path: ctx.legacy)
  end

  test "load migrates legacy project-local session when host anchor is missing", ctx do
    write_json!(ctx.legacy, %{
      "agent_uuid" => @agent_uuid,
      "continuity_token" => @continuity_token,
      "legacy_only" => true
    })

    assert {:ok, anchor} = SessionAnchor.load(path: ctx.session, legacy_path: ctx.legacy)
    assert anchor["legacy_only"] == true

    assert ctx.session |> File.read!() |> Jason.decode!() == %{
             "agent_uuid" => @agent_uuid,
             "continuity_token" => @continuity_token,
             "legacy_only" => true
           }
  end

  test "backup_for_cutover validates and writes pre-beam backup with 0600 mode", ctx do
    write_json!(ctx.session, %{
      "agent_uuid" => @agent_uuid,
      "continuity_token" => @continuity_token
    })

    assert {:ok, result} =
             SessionAnchor.backup_for_cutover(path: ctx.session, legacy_path: ctx.legacy)

    assert result.source == ctx.session
    assert result.backup == ctx.session <> ".pre-beam"
    assert result.agent_uuid == @agent_uuid
    assert File.read!(result.backup) == File.read!(ctx.session)

    stat = File.stat!(result.backup)
    assert Bitwise.band(stat.mode, 0o777) == 0o600
  end

  test "backup_for_cutover refuses to overwrite unless forced", ctx do
    write_json!(ctx.session, %{"agent_uuid" => @agent_uuid})
    File.write!(ctx.session <> ".pre-beam", "old")

    assert {:error, {:backup_exists, _path}} =
             SessionAnchor.backup_for_cutover(path: ctx.session, legacy_path: ctx.legacy)

    assert {:ok, _result} =
             SessionAnchor.backup_for_cutover(
               path: ctx.session,
               legacy_path: ctx.legacy,
               force: true
             )

    assert File.read!(ctx.session <> ".pre-beam") == File.read!(ctx.session)
  end

  test "mix sentinel.session_backup reports identity summary without token", ctx do
    write_json!(ctx.session, %{
      "agent_uuid" => @agent_uuid,
      "continuity_token" => @continuity_token
    })

    output =
      capture_io(fn ->
        Mix.Task.run("sentinel.session_backup", [
          "--session-file",
          ctx.session,
          "--legacy-session-file",
          ctx.legacy
        ])
      end)

    assert output =~ "sentinel.session_backup: ok"
    assert output =~ "source:     #{ctx.session}"
    assert output =~ "backup:     #{ctx.session}.pre-beam"
    assert output =~ "agent_uuid: #{@agent_uuid}"
    refute output =~ @continuity_token
  end

  defp write_json!(path, map) do
    File.mkdir_p!(Path.dirname(path))
    File.write!(path, Jason.encode!(map))
  end
end
