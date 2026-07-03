#!/bin/bash
# rebind-resident-session.sh <agent_uuid> <session_key> — operator recovery for
# a resident whose transport binding vanished from BOTH stores (Redis + PG).
#
# Why this exists (2026-07-03, Lumen incident + acceptance tests): binding
# durability is the PG session row, which renews +24h on every check-in — a
# Redis wipe self-heals via PATH2 with no action. But if the PG row is ALSO
# gone (>24h resident outage, DB loss), NO client call can recreate it:
# identity(resume) resolves without binding, bind_session is fail-closed on
# unknown keys, and onboard's cross-process token resume is retired (S1-c).
# That is deliberate — those gates are the F3 phantom-mint fix. The sanctioned
# recreation path is the operator, i.e. this script.
set -euo pipefail
UUID="${1:?usage: rebind-resident-session.sh <agent_uuid> <session_key>}"
KEY="${2:?usage: rebind-resident-session.sh <agent_uuid> <session_key>}"
DB="${UNITARES_DB:-governance}"

IDENTITY_ID=$(psql -d "$DB" -tA -c \
  "select identity_id from core.identities where agent_id = '$UUID' and disabled_at is null;")
if [ -z "$IDENTITY_ID" ]; then
  echo "no active identity row for agent_id=$UUID — refusing (verify the UUID first)" >&2
  exit 1
fi

psql -d "$DB" -c "
insert into core.sessions (session_id, identity_id, created_at, last_active, expires_at, client_type, client_info, is_active, metadata)
values ('$KEY', $IDENTITY_ID, now(), now(), now() + interval '24 hours', 'mcp',
        jsonb_build_object('agent_uuid', '$UUID', 'bound_via', 'operator_rebind_script', 'rebound_at', now()::text),
        true, '{}'::jsonb)
on conflict (session_id) do update
  set identity_id = excluded.identity_id, is_active = true,
      last_active = now(), expires_at = now() + interval '24 hours'
returning session_id, identity_id, expires_at;"
echo "bound. The row renews +24h on every resident check-in from here."
