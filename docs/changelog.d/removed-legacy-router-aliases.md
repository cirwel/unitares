- **37 of the last 38 legacy tool aliases** (compatibility: callable names removed).
  These were pre-consolidation tool names that each renamed one router call
  and injected its action. They now return `tool_not_found_error`, refused
  before any identity step runs. Use the router call instead: `agent`
  (`list_agents`, `get_agent_metadata`, `update_agent_metadata`,
  `archive_agent`, `delete_agent`), `observe` (`observe_agent`,
  `compare_agents`, `compare_me_to_similar`, `detect_anomalies`,
  `aggregate_metrics`), `dialectic` (`request_dialectic_review`,
  `submit_thesis`, `submit_antithesis`, `submit_synthesis`,
  `reassign_reviewer`, `get_dialectic_session`, `list_dialectic_sessions`),
  `knowledge` (`store_knowledge_graph`, `get_knowledge_graph`,
  `list_knowledge_graph`, `update_discovery_status_graph`,
  `get_discovery_details`, `cleanup_knowledge_graph`, `get_lifecycle_stats`),
  `calibration` (`check_calibration`, `update_calibration_ground_truth`,
  `backfill_calibration_from_dialectic`, `rebuild_calibration`), `export`
  (`get_system_history`, `export_to_file`) and `admin` (`get_connection_status`, `get_tool_usage_stats`, `get_telemetry_metrics`,
  `debug_request_context`, `validate_file_path`, `reset_monitor`,
  `cleanup_stale_locks`), each with the action its old name implied. No
  router, action, identity requirement or read/write class changes, and the
  advertised surface digest does not move. Server hints and tool
  descriptions that named the old tools now show the router call, and the
  router descriptions no longer list the tools they replaced. Only the
  eight workflow aliases remain, plus `get_server_info`, which stays because
  the Wave 3a BEAM route is keyed on that name. The read/write classes some of these
  aliases carried now live in `tool_meta.ACTION_OPERATIONS`, so timeout
  recovery for router reads is unchanged.
