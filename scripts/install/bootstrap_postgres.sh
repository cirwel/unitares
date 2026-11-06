#!/usr/bin/env bash
# Initialize a fresh bare-metal UNITARES PostgreSQL database in canonical order.
#
# Dry-run is the default. Pass --apply to execute DDL. This deliberately mirrors
# db/postgres/docker-initdb.sh: migrations 003-030 run before partition setup,
# while migration 031+ runs after it.

set -Eeuo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
POSTGRES_ROOT="$REPO_ROOT/db/postgres"
MIGRATIONS_ROOT="$POSTGRES_ROOT/migrations"
DB_URL="${DB_POSTGRES_URL:-postgresql://localhost:5432/governance}"
APPLY=0
INSTALL_ID=""

usage() {
    printf '%s\n' \
        "Usage: scripts/install/bootstrap_postgres.sh [--apply --install-id UUID] [--db-url DSN]" \
        "" \
        "Initializes the base schemas, numbered migrations, partitions, and AGE" \
        "graph in the same order as the Docker quickstart. Dry-run is the default." \
        "DB_POSTGRES_URL supplies the DSN when --db-url is omitted." \
        "Apply requires a new database marked by the provisioning journal." \
        "Completed or interrupted bootstraps must not be replayed."
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --apply)
            APPLY=1
            shift
            ;;
        --dry-run)
            APPLY=0
            shift
            ;;
        --install-id)
            [ "$#" -ge 2 ] || { echo 'error: --install-id requires a UUID' >&2; exit 2; }
            INSTALL_ID="$2"
            shift 2
            ;;
        --db-url)
            if [ "$#" -lt 2 ]; then
                echo "error: --db-url requires a DSN" >&2
                exit 2
            fi
            DB_URL="$2"
            shift 2
            ;;
        --help|-h)
            usage
            exit 0
            ;;
        *)
            echo "error: unknown argument: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

if [ "$APPLY" -eq 1 ] && ! [[ "$INSTALL_ID" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$ ]]; then
    echo 'error: fresh bootstrap requires --install-id from unitares setup --homebrew --provision-db; use migrations for an existing installation.' >&2
    exit 2
fi

if [ "$APPLY" -eq 1 ] && ! command -v psql >/dev/null 2>&1; then
    echo "error: psql is required; install and start postgresql@17 first" >&2
    exit 2
fi

# A startup file must not reconnect this explicit bootstrap to another DB.
PSQL=(psql -X -v ON_ERROR_STOP=1 "$DB_URL")

if [ "$APPLY" -eq 1 ]; then
    # The guard is here, not only in the Homebrew caller. A durable running
    # marker prevents a second bootstrap or an unsafe replay after interruption.
    # This transaction serializes the marker transition across bootstrap callers.
    "${PSQL[@]}" -q -v install_id="$INSTALL_ID" <<'SQL'
BEGIN;
SELECT pg_advisory_xact_lock(174936, hashtext(current_database()));
SELECT set_config('unitares.bootstrap_install', :'install_id', true);
DO $$
DECLARE expected text := 'unitares-install:' || current_setting('unitares.bootstrap_install');
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_database WHERE datname = current_database()
        AND datdba = (SELECT oid FROM pg_roles WHERE rolname = current_user)
        AND shobj_description(oid, 'pg_database') = expected || ':prepared'
    ) THEN
        RAISE EXCEPTION 'Bootstrap refused: database is not newly prepared for this install. Preserve existing data; use migrations or inspect interrupted setup.';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_namespace WHERE nspname NOT IN ('public', 'ag_catalog', 'information_schema') AND nspname NOT LIKE 'pg_%')
       OR EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public') THEN
        RAISE EXCEPTION 'Bootstrap refused: unexpected objects in prepared database.';
    END IF;
    EXECUTE format('COMMENT ON DATABASE %I IS %L', current_database(), expected || ':bootstrapping');
END $$;
COMMIT;
SQL
fi

run_file() {
    local relative="$1"
    if [ "$APPLY" -eq 0 ]; then
        printf 'would apply %s\n' "$relative"
        return
    fi
    printf 'applying %s\n' "$relative"
    (
        cd "$REPO_ROOT"
        "${PSQL[@]}" -f "$POSTGRES_ROOT/$relative"
    )
}

register_knowledge_schema() {
    if [ "$APPLY" -eq 0 ]; then
        printf '%s\n' "would register migration 002_knowledge_schema.sql"
        return
    fi
    "${PSQL[@]}" -q -c \
        "INSERT INTO core.schema_migrations (version, name, applied_at) VALUES (2, 'knowledge_schema', NOW()) ON CONFLICT (version) DO NOTHING;"
}

migration_is_applied() {
    local version="$1"
    [ "$("${PSQL[@]}" -Atqc "SELECT 1 FROM core.schema_migrations WHERE version = $version")" = "1" ]
}

record_checksum_if_supported() {
    local version="$1" path="$2" column_exists checksum
    column_exists="$("${PSQL[@]}" -Atqc \
        "SELECT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = 'core' AND table_name = 'schema_migrations' AND column_name = 'checksum')")"
    [ "$column_exists" = "t" ] || return 0

    checksum="$(shasum -a 256 "$path" | awk '{print $1}')"
    # psql expands variables in stdin/file input, but not in a -c SQL command.
    "${PSQL[@]}" \
        -v migration_version="$version" \
        -v migration_checksum="$checksum" \
        -q <<'SQL'
UPDATE core.schema_migrations SET checksum = :'migration_checksum' WHERE version = :migration_version AND checksum IS NULL;
SQL
}

run_migration() {
    local path="$1" base version_raw version relative
    base="$(basename "$path")"
    version_raw="${base%%_*}"
    case "$version_raw" in
        ''|*[!0-9]*) return 0 ;;
    esac
    version=$((10#$version_raw))
    relative="migrations/$base"

    if [ "$APPLY" -eq 0 ]; then
        printf 'would apply %s\n' "$relative"
        return
    fi
    if migration_is_applied "$version"; then
        printf 'already applied %s\n' "$relative"
        return
    fi

    run_file "$relative"
    record_checksum_if_supported "$version" "$path"
}

echo "UNITARES bare-metal PostgreSQL bootstrap"
if [ "$APPLY" -eq 0 ]; then
    echo "mode: dry-run (pass --apply to execute)"
else
    echo "mode: apply"
fi

run_file "init-extensions.sql"
run_file "schema.sql"
run_file "knowledge_schema.sql"
register_knowledge_schema
run_file "embeddings_schema.sql"
run_file "embeddings_bge_m3_schema.sql"

for migration in "$MIGRATIONS_ROOT"/*.sql; do
    base="$(basename "$migration")"
    version_raw="${base%%_*}"
    case "$version_raw" in
        ''|*[!0-9]*) continue ;;
    esac
    version=$((10#$version_raw))
    if [ "$version" -ge 3 ] && [ "$version" -le 30 ]; then
        run_migration "$migration"
    fi
done

run_file "partitions.sql"
run_file "graph_schema.sql"

for migration in "$MIGRATIONS_ROOT"/*.sql; do
    base="$(basename "$migration")"
    version_raw="${base%%_*}"
    case "$version_raw" in
        ''|*[!0-9]*) continue ;;
    esac
    version=$((10#$version_raw))
    if [ "$version" -ge 31 ]; then
        run_migration "$migration"
    fi
done

if [ "$APPLY" -eq 1 ]; then
    python3 "$REPO_ROOT/scripts/dev/apply_migrations.py" \
        --db-url "$DB_URL" --check
    "${PSQL[@]}" -q -v install_id="$INSTALL_ID" <<'SQL'
BEGIN;
SELECT pg_advisory_xact_lock(174936, hashtext(current_database()));
SELECT set_config('unitares.bootstrap_install', :'install_id', true);
DO $$
DECLARE expected text := 'unitares-install:' || current_setting('unitares.bootstrap_install');
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_database WHERE datname=current_database()
        AND datdba=(SELECT oid FROM pg_roles WHERE rolname=current_user)
        AND shobj_description(oid,'pg_database')=expected || ':bootstrapping') THEN
        RAISE EXCEPTION 'Bootstrap completion refused: ownership marker changed.';
    END IF;
    EXECUTE format('COMMENT ON DATABASE %I IS %L', current_database(), expected || ':bootstrapped');
END $$;
COMMIT;
SQL
    echo "UNITARES PostgreSQL bootstrap complete"
fi
