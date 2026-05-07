defmodule UnitaresSentinel.FindingsTest do
  use ExUnit.Case, async: true

  alias UnitaresSentinel.Findings

  defp alarm do
    %{
      kind: "ad_hoc",
      severity: "high",
      summary: "forced release: dialectic:/x (lease lease-1)",
      fingerprint: "forced_release:ad_hoc:event-1",
      extra: %{
        event_id: "event-1",
        ts: "2026-05-06T00:00:00Z",
        lease_id: "lease-1",
        surface_id: "dialectic:/x",
        surface_kind: "dialectic",
        fingerprint: "spoofed"
      }
    }
  end

  test "alarm_body mirrors Python forced-release post_finding shape" do
    body = Findings.alarm_body(alarm(), agent_id: "sentinel-test", agent_name: "Sentinel")

    assert body["type"] == "sentinel_alarm_finding"
    assert body["severity"] == "high"
    assert body["message"] == "forced release: dialectic:/x (lease lease-1)"
    assert body["agent_id"] == "sentinel-test"
    assert body["agent_name"] == "Sentinel"
    assert body["fingerprint"] == "forced_release:ad_hoc:event-1"
    assert body["alarm_kind"] == "ad_hoc"
    assert body["event_id"] == "event-1"
    assert body["surface_kind"] == "dialectic"
  end

  # Regression for the http_api.py /api/findings suffix gate
  # (`_FINDING_TYPE_SUFFIX = "_finding"`, src/http_api.py:1090). Pre-fix the
  # alarm `type` was `"sentinel_forced_release_alarm"`, which fails the
  # suffix check and was silently 400'd at the HTTP boundary — a Sentinel
  # forced-release alarm at 16:46:44 on 2026-05-06 surfaced this bug. Pin the
  # contract in this layer; finding/alarm bodies must produce a `_finding`-
  # suffixed `type` so the governance gateway accepts them. The granular
  # alarm kind continues to ride in the `alarm_kind` field for downstream
  # consumers.
  test "alarm_body and finding_body types satisfy /api/findings suffix gate" do
    alarm_body = Findings.alarm_body(alarm())
    assert String.ends_with?(alarm_body["type"], "_finding"),
           "alarm_body type must end in _finding (governance /api/findings gate); got #{inspect(alarm_body["type"])}"
    assert alarm_body["alarm_kind"] == "ad_hoc",
           "alarm_kind must still carry the granular kind so downstream consumers don't lose signal"

    finding_body =
      Findings.finding_body(%{
        type: "coordinated_degradation",
        violation_class: "BEH",
        severity: "high",
        summary: "regression body"
      })

    assert String.ends_with?(finding_body["type"], "_finding"),
           "finding_body type must end in _finding (governance /api/findings gate); got #{inspect(finding_body["type"])}"
  end

  test "finding_body mirrors Python sentinel_finding shape and fingerprint" do
    body =
      Findings.finding_body(
        %{
          type: "coordinated_degradation",
          violation_class: "BEH",
          severity: "high",
          summary: "3 agents drifting in lockstep"
        },
        agent_id: "sentinel-test-uuid",
        agent_name: "Sentinel"
      )

    assert body["type"] == "sentinel_finding"
    assert body["severity"] == "high"
    assert body["message"] == "3 agents drifting in lockstep"
    assert body["agent_id"] == "sentinel-test-uuid"
    assert body["agent_name"] == "Sentinel"
    assert body["violation_class"] == "BEH"
    assert body["finding_type"] == "coordinated_degradation"
    assert body["fingerprint"] == "da9b8e957ab6971e"
  end

  test "post_finding returns true only for accepted non-deduped response" do
    http_post = fn url, body, headers, timeout_ms ->
      assert url == "http://example.test/api/findings"
      assert body["type"] == "sentinel_finding"
      assert body["finding_type"] == "verdict_shift"
      assert {"Content-Type", "application/json"} in headers
      assert timeout_ms == 123

      {:ok, 200, ~s({"success":true,"deduped":false})}
    end

    assert Findings.post_finding(
             %{
               type: "verdict_shift",
               violation_class: "ENT",
               severity: "high",
               summary: "Pause rate 40% in last 10min (2/5)"
             },
             url: "http://example.test/api/findings",
             timeout_ms: 123,
             agent_id: "sentinel-test-uuid",
             http_post: http_post
           )
  end

  test "post_alarm returns true only for accepted non-deduped response" do
    http_post = fn url, body, headers, timeout_ms ->
      assert url == "http://example.test/api/findings"
      assert body["type"] == "sentinel_alarm_finding"
      assert {"Content-Type", "application/json"} in headers
      assert timeout_ms == 123

      {:ok, 200, ~s({"success":true,"deduped":false})}
    end

    assert Findings.post_alarm(
             alarm(),
             url: "http://example.test/api/findings",
             timeout_ms: 123,
             http_post: http_post
           )
  end

  test "post_alarm swallows transport failures" do
    http_post = fn _url, _body, _headers, _timeout_ms -> raise "connection refused" end

    refute Findings.post_alarm(alarm(), http_post: http_post)
  end

  test "post_alarm returns false for deduped responses" do
    http_post = fn _url, _body, _headers, _timeout_ms ->
      {:ok, 200, ~s({"success":true,"deduped":true})}
    end

    refute Findings.post_alarm(alarm(), http_post: http_post)
  end
end
