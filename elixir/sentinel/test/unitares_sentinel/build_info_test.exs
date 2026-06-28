defmodule UnitaresSentinel.BuildInfoTest do
  @moduledoc """
  Pure tests for the boot build-stamp: BuildInfo shape/caching and the
  `Findings.post_build_info/2` payload. No DB or network — the findings POST
  is exercised through an injected `:http_post`.
  """

  use ExUnit.Case, async: true

  alias UnitaresSentinel.{BuildInfo, Findings}

  describe "BuildInfo.info/0" do
    test "returns version, sha, dirty, summary with expected shapes" do
      info = BuildInfo.info()

      assert is_binary(info.version)
      assert is_binary(info.sha)
      assert is_boolean(info.dirty)
      assert is_binary(info.summary)
      # Summary is self-describing: app name + version + sha marker.
      assert info.summary =~ "unitares_sentinel"
      assert info.summary =~ info.version
      assert info.summary =~ "@"
    end

    test "is cached — repeat calls return the identical map" do
      assert BuildInfo.info() == BuildInfo.info()
    end

    test "convenience accessors agree with info/0" do
      info = BuildInfo.info()
      assert BuildInfo.version() == info.version
      assert BuildInfo.sha() == info.sha
      assert BuildInfo.summary() == info.summary
    end
  end

  describe "Findings.post_build_info/2" do
    test "posts a sentinel_build_finding with the sha in body + fingerprint" do
      parent = self()

      http_post = fn _url, body, _headers, _timeout ->
        send(parent, {:posted, body})
        {:ok, 200, ~s({"success": true})}
      end

      info = %{
        version: "0.1.0",
        sha: "abc123def456",
        dirty: false,
        summary: "unitares_sentinel 0.1.0 @abc123def456"
      }

      assert Findings.post_build_info(info, http_post: http_post) == true

      assert_receive {:posted, body}
      # Gateway requires the `_finding` suffix on the event type.
      assert body["type"] == "sentinel_build_finding"
      assert body["severity"] == "info"
      assert body["git_sha"] == "abc123def456"
      assert body["version"] == "0.1.0"
      assert body["dirty"] == false
      assert body["message"] =~ "abc123def456"
      # Fingerprint keys on the sha → new code = fresh finding, same code dedups.
      assert body["fingerprint"] == Findings.compute_fingerprint(["sentinel", "build", "abc123def456"])
    end

    test "a different sha yields a different fingerprint (dedup distinctness)" do
      capture = fn ->
        parent = self()

        http_post = fn _url, body, _headers, _timeout ->
          send(parent, {:fp, body["fingerprint"]})
          {:ok, 200, ~s({"success": true})}
        end

        &Findings.post_build_info(&1, http_post: http_post)
      end

      poster = capture.()
      poster.(%{version: "0.1.0", sha: "aaaaaaaaaaaa", dirty: false, summary: "s @aaaaaaaaaaaa"})
      poster.(%{version: "0.1.0", sha: "bbbbbbbbbbbb", dirty: false, summary: "s @bbbbbbbbbbbb"})

      assert_receive {:fp, fp_a}
      assert_receive {:fp, fp_b}
      assert fp_a != fp_b
    end
  end
end
