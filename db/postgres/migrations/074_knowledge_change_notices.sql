-- A coalescing, replayable pointer queue, coupled to canonical KG writes.
-- These are delivery hints, never the knowledge/audit record. No old rows
-- are backfilled and no knowledge content is copied or deleted.
BEGIN;
CREATE TABLE IF NOT EXISTS knowledge.notice_preferences (
    recipient text PRIMARY KEY,
    muted boolean NOT NULL DEFAULT false,
    gap_token uuid
);
CREATE TABLE IF NOT EXISTS knowledge.change_notices (
    recipient text NOT NULL,
    discovery_id text NOT NULL REFERENCES knowledge.discoveries(id) ON DELETE CASCADE,
    revision uuid NOT NULL DEFAULT gen_random_uuid(),
    changed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    change_kind text NOT NULL,
    changed_fields text[] NOT NULL,
    PRIMARY KEY (recipient, discovery_id)
);
CREATE INDEX IF NOT EXISTS change_notices_recipient_time
    ON knowledge.change_notices (recipient, changed_at);

CREATE OR REPLACE FUNCTION knowledge.trim_change_notices(p_recipient text)
RETURNS void LANGUAGE plpgsql AS $$
DECLARE removed integer;
BEGIN
    -- Serialize this recipient's queue maintenance. Reads do not claim rows.
    INSERT INTO knowledge.notice_preferences(recipient) VALUES (p_recipient)
        ON CONFLICT DO NOTHING;
    PERFORM 1 FROM knowledge.notice_preferences WHERE recipient=p_recipient FOR UPDATE;
    WITH expired AS (
        SELECT discovery_id FROM knowledge.change_notices
        WHERE recipient=p_recipient AND changed_at < clock_timestamp()-interval '7 days'
        UNION
        SELECT discovery_id FROM (
            SELECT discovery_id FROM knowledge.change_notices
            WHERE recipient=p_recipient ORDER BY changed_at DESC, discovery_id OFFSET 100
        ) overflow
    )
    DELETE FROM knowledge.change_notices n USING expired e
        WHERE n.recipient=p_recipient AND n.discovery_id=e.discovery_id;
    GET DIAGNOSTICS removed = ROW_COUNT;
    IF removed > 0 THEN
        UPDATE knowledge.notice_preferences SET gap_token=gen_random_uuid()
            WHERE recipient=p_recipient;
    END IF;
END $$;

CREATE OR REPLACE FUNCTION knowledge.capture_change_notice()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE recipients text[]; lock_recipients text[]; target text; changed text[];
BEGIN
    IF TG_OP='UPDATE' THEN
        SELECT array_agg(n.key ORDER BY n.key) INTO changed
        FROM jsonb_each(to_jsonb(NEW)) n
        WHERE n.key NOT IN ('updated_at', 'search_vector')
          AND n.value IS DISTINCT FROM to_jsonb(OLD)->n.key;
        IF changed IS NULL THEN RETURN NEW; END IF;
    ELSE
        changed := ARRAY['created'];
    END IF;
    -- Exact UUID tags only; prose and display names cannot address a session.
    SELECT array_agg(DISTINCT r ORDER BY r) INTO recipients FROM (
        SELECT NEW.agent_id AS r WHERE TG_OP='UPDATE'
        UNION ALL
        SELECT substring(t FROM 14) FROM unnest(NEW.tags) t
        WHERE t ~ '^notify-agent:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
    ) q WHERE r IS NOT NULL AND r <> '';
    -- Lock old and new recipients before deleting queue rows. The reader takes
    -- the same preference-before-queue order, so ack cannot deadlock this write.
    SELECT array_agg(DISTINCT r ORDER BY r) INTO lock_recipients FROM (
        SELECT unnest(coalesce(recipients, ARRAY[]::text[])) AS r
        UNION ALL
        SELECT recipient FROM knowledge.change_notices WHERE discovery_id=NEW.id
    ) q;
    FOREACH target IN ARRAY coalesce(lock_recipients, ARRAY[]::text[]) LOOP
        INSERT INTO knowledge.notice_preferences(recipient) VALUES(target) ON CONFLICT DO NOTHING;
        PERFORM 1 FROM knowledge.notice_preferences WHERE recipient=target FOR UPDATE;
    END LOOP;
    -- A removed mention must not leave a pointer to the formerly addressed row.
    DELETE FROM knowledge.change_notices WHERE discovery_id=NEW.id
        AND NOT (recipient=ANY(coalesce(recipients, ARRAY[]::text[])));
    FOREACH target IN ARRAY coalesce(recipients, ARRAY[]::text[]) LOOP
        INSERT INTO knowledge.change_notices(recipient, discovery_id, change_kind, changed_fields)
            VALUES(target, NEW.id, lower(TG_OP), changed)
        ON CONFLICT (recipient, discovery_id) DO UPDATE SET
            revision=gen_random_uuid(), changed_at=clock_timestamp(),
            change_kind=EXCLUDED.change_kind,
            changed_fields=(SELECT array_agg(DISTINCT f ORDER BY f)
                FROM unnest(knowledge.change_notices.changed_fields || EXCLUDED.changed_fields) f);
        PERFORM knowledge.trim_change_notices(target);
    END LOOP;
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS discoveries_change_notice ON knowledge.discoveries;
CREATE TRIGGER discoveries_change_notice AFTER INSERT OR UPDATE ON knowledge.discoveries
    FOR EACH ROW EXECUTE FUNCTION knowledge.capture_change_notice();
INSERT INTO core.schema_migrations(version, name) VALUES (74, 'knowledge_change_notices')
    ON CONFLICT (version) DO NOTHING;
COMMIT;
