"""Watcher noise narrowing, 2026-09-24.

A 30-day audit found 0 of 16 human verdicts on Watcher findings confirmed.
Lifetime record in findings.jsonl:

- P016: 82 findings, 0 confirmed, 63 on test files. Demoted to experimental.
- P006: 39 findings, 0 confirmed; several flagged `except` clauses already
  carried `# noqa: BLE001`, and two were in tests.
- Watcher tests under tests/ appended fixture lines to the operator's real
  ~/Library/Logs/unitares-watcher.log.
"""

from __future__ import annotations

import os
from pathlib import Path

from agents.watcher.agent import (
    _verify_finding_against_source,
    load_pattern_ids,
    load_pattern_severities,
)
from agents.watcher.findings import Finding


def _make(pattern: str, file: str, line: int) -> Finding:
    return Finding(
        pattern=pattern,
        file=file,
        line=line,
        hint="x",
        severity="medium",
        detected_at="2026-09-24T00:00:00Z",
        model_used="test",
    )


class TestP016Demoted:
    def test_p016_is_not_an_active_pattern(self):
        # No severity => parse_findings drops a model-returned P016.
        assert "P016" not in load_pattern_severities()

    def test_p016_still_documented_as_experimental(self):
        # The prompt still shows it, so a model quoting it reads as working.
        assert "EXP-P016" in load_pattern_ids()


class TestP006Acknowledged:
    def test_noqa_ble001_on_clause_dropped(self):
        snippet = {
            10: "    try:",
            11: "        bootstrap()",
            12: "    except Exception as err:  # noqa: BLE001 — bootstrap is fail-open",
            13: "        logger.debug(f'bootstrap skipped: {err}')",
        }
        assert _verify_finding_against_source(_make("P006", "src/x.py", 12), "", snippet) is False

    def test_body_line_under_noqa_clause_dropped(self):
        snippet = {
            12: "    except Exception as err:  # noqa: BLE001",
            13: "        logger.debug(f'bootstrap skipped: {err}')",
        }
        assert _verify_finding_against_source(_make("P006", "src/x.py", 13), "", snippet) is False

    def test_bare_noqa_dropped(self):
        snippet = {5: "    except Exception:  # noqa", 6: "        pass"}
        assert _verify_finding_against_source(_make("P006", "src/x.py", 5), "", snippet) is False

    def test_unrelated_noqa_code_still_fires(self):
        snippet = {5: "    except Exception:  # noqa: E501", 6: "        pass"}
        assert _verify_finding_against_source(_make("P006", "src/x.py", 5), "", snippet) is True

    def test_reraise_in_body_dropped(self):
        snippet = {
            20: "    except Exception as exc:",
            21: "        logger.warning(f'write failed: {exc}')",
            22: "        raise",
            23: "    return result",
        }
        assert _verify_finding_against_source(_make("P006", "src/x.py", 20), "", snippet) is False

    def test_raise_after_body_does_not_count(self):
        snippet = {
            20: "        except Exception:",
            21: "            pass",
            22: "        raise ValueError('unrelated')",
        }
        assert _verify_finding_against_source(_make("P006", "src/x.py", 20), "", snippet) is True

    def test_plain_swallow_still_fires(self):
        snippet = {
            30: "    except Exception as e:",
            31: "        logger.debug(f'skipped: {e}')",
            32: "    return None",
        }
        assert _verify_finding_against_source(_make("P006", "src/x.py", 30), "", snippet) is True

    def test_p006_in_tests_dropped(self):
        snippet = {88: "    except Exception:", 89: "        pass"}
        finding = _make("P006", "/Users/x/projects/unitares/tests/test_db_utils.py", 88)
        assert _verify_finding_against_source(finding, "", snippet) is False


class TestWatcherLogIsolation:
    def test_suite_sets_log_override_outside_real_log(self):
        real = Path.home() / "Library" / "Logs" / "unitares-watcher.log"
        override = os.environ.get("UNITARES_WATCHER_LOG_FILE")
        assert override and Path(override) != real

    def test_log_writes_to_override(self, tmp_path, monkeypatch):
        from agents.watcher import _util

        target = tmp_path / "w.log"
        monkeypatch.setenv("UNITARES_WATCHER_LOG_FILE", str(target))
        _util.log("probe", "info")
        assert "probe" in target.read_text()
