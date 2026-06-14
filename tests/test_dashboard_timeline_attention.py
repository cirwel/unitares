"""Regression coverage for the dashboard Activity attention filter."""

from __future__ import annotations

import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent


def test_activity_filter_label_names_attention_view() -> None:
    html = (ROOT / "dashboard" / "index.html").read_text()
    assert '<option value="important">Needs attention</option>' in html


@pytest.mark.skipif(
    shutil.which("node") is None,
    reason="node is required for dashboard JS regression",
)
def test_activity_filter_defaults_to_attention_events(tmp_path: Path) -> None:
    """Run timeline.js with a tiny DOM and pin the product-level filter contract."""
    script = tmp_path / "timeline_attention_contract.cjs"
    timeline_path = ROOT / "dashboard" / "timeline.js"

    script.write_text(
        textwrap.dedent(
            f"""
            const fs = require("fs");
            const vm = require("vm");

            function assertIncludes(value, needle) {{
              if (!value.includes(needle)) {{
                throw new Error(`Expected ${{JSON.stringify(needle)}} in ${{value}}`);
              }}
            }}

            function assertNotIncludes(value, needle) {{
              if (value.includes(needle)) {{
                throw new Error(`Did not expect ${{JSON.stringify(needle)}} in ${{value}}`);
              }}
            }}

            function makeElement(id, value) {{
              return {{
                id,
                value: value || "",
                innerHTML: "",
                className: "",
                title: "",
                textContent: "",
                listeners: {{}},
                addEventListener(event, callback) {{
                  this.listeners[event] = callback;
                }},
                dispatch(event) {{
                  this.listeners[event].call(this, {{ target: this }});
                }}
              }};
            }}

            const elements = {{
              "timeline-container": makeElement("timeline-container"),
              "timeline-filter": makeElement("timeline-filter", "important"),
              "timeline-clear": makeElement("timeline-clear")
            }};

            const context = {{
              console,
              Date,
              URLSearchParams,
              DashboardState: {{}},
              VERDICT_ICONS: {{ proceed: "ok", caution: "warn", pause: "stop" }},
              DataProcessor: {{
                escapeHtml(value) {{
                  return String(value || "")
                    .replace(/&/g, "&amp;")
                    .replace(/</g, "&lt;")
                    .replace(/>/g, "&gt;")
                    .replace(/"/g, "&quot;");
                }},
                formatRelativeTime() {{
                  return "";
                }},
                formatTimestamp() {{
                  return "12:00";
                }}
              }},
              document: {{
                readyState: "complete",
                getElementById(id) {{
                  return elements[id] || null;
                }},
                querySelector() {{
                  return null;
                }},
                addEventListener(event, callback) {{
                  if (event === "DOMContentLoaded") callback();
                }}
              }},
              localStorage: {{
                getItem() {{
                  return null;
                }}
              }},
              fetch() {{
                return Promise.resolve({{ ok: false }});
              }}
            }};
            context.window = context;
            context.window.location = {{ search: "" }};

            vm.createContext(context);
            vm.runInContext(
              fs.readFileSync({json.dumps(str(timeline_path))}, "utf8"),
              context,
              {{ filename: "dashboard/timeline.js" }}
            );

            const timeline = context.TimelineModule;
            timeline.onEISVUpdate({{
              type: "eisv_update",
              agent_label: "green-agent",
              verdict: "proceed",
              timestamp: "2026-06-14T12:00:00Z"
            }});
            timeline.onGovernanceEvent({{
              type: "knowledge_read",
              agent_label: "reader",
              timestamp: "2026-06-14T12:01:00Z"
            }});
            timeline.onGovernanceEvent({{
              type: "knowledge_write",
              agent_label: "writer",
              severity: "low",
              discovery_type: "note",
              summary: "routine note",
              timestamp: "2026-06-14T12:02:00Z"
            }});
            timeline.onGovernanceEvent({{
              type: "knowledge_write",
              agent_label: "writer",
              severity: "medium",
              discovery_type: "bug_found",
              summary: "medium priority incident",
              timestamp: "2026-06-14T12:03:00Z"
            }});
            timeline.onGovernanceEvent({{
              type: "knowledge_write",
              agent_label: "writer",
              severity: "high",
              discovery_type: "bug_found",
              summary: "high priority incident",
              timestamp: "2026-06-14T12:04:00Z"
            }});
            timeline.onGovernanceEvent({{
              type: "knowledge_write",
              agent_label: "writer",
              severity: "critical",
              discovery_type: "bug_found",
              summary: "critical priority incident",
              timestamp: "2026-06-14T12:05:00Z"
            }});

            const attentionHtml = elements["timeline-container"].innerHTML;
            assertIncludes(attentionHtml, 'data-attention="attention"');
            assertIncludes(attentionHtml, "medium priority incident");
            assertIncludes(attentionHtml, "high priority incident");
            assertIncludes(attentionHtml, "critical priority incident");
            assertIncludes(attentionHtml, "warn caution");
            assertIncludes(attentionHtml, "stop pause");
            assertNotIncludes(attentionHtml, "checked in");
            assertNotIncludes(attentionHtml, "knowledge read");
            assertNotIncludes(attentionHtml, "routine note");

            elements["timeline-filter"].value = "all";
            elements["timeline-filter"].dispatch("change");

            const allHtml = elements["timeline-container"].innerHTML;
            assertIncludes(allHtml, "checked in");
            assertIncludes(allHtml, "knowledge read");
            assertIncludes(allHtml, "routine note");
            assertIncludes(allHtml, "medium priority incident");
            assertIncludes(allHtml, "high priority incident");
            assertIncludes(allHtml, "critical priority incident");
            """
        )
    )

    result = subprocess.run(
        ["node", str(script)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
