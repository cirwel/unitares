"""Regression coverage for dashboard agent lineage redaction."""

from __future__ import annotations

import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.skipif(
    shutil.which("node") is None,
    reason="node is required for dashboard JS regression",
)
def test_agents_lineage_redaction_does_not_link_or_render_redacted_ids(tmp_path: Path) -> None:
    """Run agents.js with a tiny DOM and pin redacted lineage rendering."""
    script = tmp_path / "agents_lineage_redaction_contract.cjs"
    agents_path = ROOT / "dashboard" / "agents.js"

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

            function makeElement(id) {{
              return {{
                id,
                value: "",
                innerHTML: "",
                textContent: "",
                className: "",
                title: "",
                style: {{}},
                classList: {{
                  add() {{}},
                  remove() {{}}
                }},
                listeners: {{}},
                addEventListener(event, callback) {{
                  this.listeners[event] = callback;
                }},
                querySelector() {{
                  return null;
                }},
                querySelectorAll() {{
                  return [];
                }}
              }};
            }}

            const elements = {{
              "agents-container": makeElement("agents-container"),
              "agents-filter-info": makeElement("agents-filter-info"),
              "panel-modal": makeElement("panel-modal"),
              "modal-title": makeElement("modal-title"),
              "modal-body": makeElement("modal-body")
            }};

            const visibleParentUuid = "11111111-1111-4111-8111-111111111111";
            const visibleChildUuid = "22222222-2222-4222-8222-222222222222";
            const redactedLabel = "Parent";
            const redactedChildUuid = "33333333-3333-4333-8333-333333333333";

            const agents = [
              {{
                agent_id: visibleParentUuid,
                label: "Visible Parent",
                lifecycle_status: "active",
                total_updates: 4
              }},
              {{
                agent_id: visibleChildUuid,
                label: "Visible Child",
                parent_agent_id: visibleParentUuid,
                spawn_reason: "subagent",
                lifecycle_status: "active",
                total_updates: 2
              }},
              {{
                agent_id: redactedChildUuid,
                label: "Redacted Child",
                parent_agent_id: redactedLabel,
                parent_agent_id_redacted: true,
                lifecycle_status: "active",
                total_updates: 2
              }},
              {{
                agent_id: redactedLabel,
                label: "Redacted Parent Alias",
                agent_id_redacted: true,
                lifecycle_status: "active",
                total_updates: 1
              }}
            ];

            const stateValues = {{
              cachedAgents: agents,
              agentEISVHistory: {{}},
              agentPageSize: 20,
              pinnedAgentId: null,
              cachedDiscoveries: []
            }};

            const context = {{
              console,
              Date,
              DashboardState: {{}},
              state: {{
                get(key) {{
                  return stateValues[key];
                }},
                set(key, value) {{
                  stateValues[key] = value;
                }}
              }},
              VERDICT_ICONS: {{}},
              MetricColors: {{
                forValue() {{
                  return "rgb(0, 0, 0)";
                }}
              }},
              DataProcessor: {{
                escapeHtml(value) {{
                  return String(value || "")
                    .replace(/&/g, "&amp;")
                    .replace(/</g, "&lt;")
                    .replace(/>/g, "&gt;")
                    .replace(/"/g, "&quot;");
                }},
                highlightMatch(value) {{
                  return String(value || "");
                }},
                formatRelativeTime() {{
                  return "";
                }},
                formatTimestamp(value) {{
                  return String(value || "");
                }},
                formatEISVMetric(value) {{
                  return {{ display: Number(value).toFixed(3), interpretation: "", color: "black" }};
                }}
              }},
              document: {{
                body: {{ style: {{}} }},
                getElementById(id) {{
                  return elements[id] || null;
                }},
                querySelector() {{
                  return null;
                }},
                querySelectorAll() {{
                  return [];
                }},
                addEventListener() {{}}
              }},
              requestAnimationFrame(callback) {{
                callback();
              }},
              setTimeout() {{}},
              CSS: {{
                escape(value) {{
                  return String(value || "");
                }}
              }}
            }};
            context.window = context;

            vm.createContext(context);
            vm.runInContext(
              fs.readFileSync({json.dumps(str(agents_path))}, "utf8"),
              context,
              {{ filename: "dashboard/agents.js" }}
            );

            const agentsModule = context.AgentsModule;
            agentsModule.renderAgentsList(agents, "");
            const listHtml = elements["agents-container"].innerHTML;

            assertIncludes(listHtml, "Visible Parent");
            assertIncludes(listHtml, "Visible Child");
            assertIncludes(listHtml, "Lineage redacted");
            assertIncludes(listHtml, `data-parent-uuid="${{visibleParentUuid}}"`);
            assertNotIncludes(listHtml, `data-parent-uuid="${{redactedLabel}}"`);
            assertNotIncludes(listHtml, `title="Spawned from ${{redactedLabel}}`);

            agentsModule.showAgentDetail(agents[2]);
            const detailHtml = elements["modal-body"].innerHTML;
            assertIncludes(detailHtml, "Redacted in this view");
            assertNotIncludes(detailHtml, `<span class="text-secondary-xs">${{redactedLabel}}</span>`);

            agentsModule.showAgentDetail(agents[0]);
            const parentDetailHtml = elements["modal-body"].innerHTML;
            assertIncludes(parentDetailHtml, "Visible Child");
            assertNotIncludes(parentDetailHtml, "Redacted Child");
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
