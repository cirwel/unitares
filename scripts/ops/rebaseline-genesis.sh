#!/bin/bash
# rebaseline-genesis.sh <agent_uuid> <reason> — operator-attested trajectory
# genesis (Σ₀) re-baseline for a sanctioned client migration / embodiment change.
#
# Why this exists (2026-07-24, #1370): genesis is immutable at trust tier 2+ —
# correct anti-hijack posture — but a sanctioned client cutover (e.g. the
# Python bridge → Elixir broker migration) changes the trajectory fingerprint
# of the SAME creature, pinning lineage_similarity below threshold forever and
# firing identity_drift on every check-in. No client call may reseed genesis
# BY DESIGN (same fail-closed philosophy as rebind-resident-session.sh); the
# sanctioned recreation path is the operator, i.e. this script.
#
# What it does, atomically per statement:
#   1. archives the current trajectory_genesis into trajectory_genesis_history[]
#      (nothing is destroyed — the old Σ₀ stays inspectable)
#   2. seeds trajectory_genesis from trajectory_current, which must be fresh
#      (<24h) so the new baseline reflects the live client, not a stale ghost
#   3. emits an explicit genesis_rebaselined audit event carrying the reason
#
# The next check-in recomputes lineage_similarity against the new Σ₀.
set -euo pipefail
UUID="${1:?usage: rebaseline-genesis.sh <agent_uuid> <reason>}"
REASON="${2:?usage: rebaseline-genesis.sh <agent_uuid> <reason>}"
DB="${UNITARES_DB:-governance}"

CHECK=$(psql -d "$DB" -tA -c "
select (metadata ? 'trajectory_genesis')::int
       + ((metadata ? 'trajectory_current')::int * 2)
       + (coalesce((metadata->>'trajectory_updated_at')::timestamptz
                    >= now() - interval '24 hours', false))::int * 4
from core.identities where agent_id = '$UUID' and disabled_at is null;")
if [ -z "$CHECK" ]; then
  echo "no active identity row for agent_id=$UUID — refusing (verify the UUID first)" >&2
  exit 1
fi
if [ "$CHECK" -ne 7 ]; then
  echo "preconditions not met (flags=$CHECK; need genesis=1 + current=2 + fresh<24h=4 = 7)." >&2
  echo "The agent must have a genesis, a current signature, and a check-in in the last 24h." >&2
  exit 1
fi

psql -d "$DB" -c "
update core.identities set metadata =
  jsonb_set(
    jsonb_set(
      jsonb_set(
        metadata,
        '{trajectory_genesis_history}',
        coalesce(metadata->'trajectory_genesis_history', '[]'::jsonb)
          || jsonb_build_array(jsonb_build_object(
               'genesis', metadata->'trajectory_genesis',
               'archived_at', now()::text,
               'reason', '$REASON',
               'attested_via', 'operator_rebaseline_script')),
        true),
      '{trajectory_genesis}', metadata->'trajectory_current', true),
    '{trajectory_genesis_at}', to_jsonb(now()::text), true),
  updated_at = now()
where agent_id = '$UUID' and disabled_at is null
returning identity_id;"

psql -d "$DB" -c "
insert into audit.events (ts, agent_id, event_type, payload)
values (now(), '$UUID', 'genesis_rebaselined',
        jsonb_build_object(
          'reason', '$REASON',
          'attested_via', 'operator_rebaseline_script',
          'genesis_history_depth',
            (select jsonb_array_length(metadata->'trajectory_genesis_history')
             from core.identities where agent_id = '$UUID')))
returning event_id, event_type;"

echo "rebaselined. The next check-in scores lineage_similarity against the new Σ₀."
