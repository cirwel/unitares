"""POST /v1/external/events — operator-gated ingestion for external GitHub events.

Option B write path for the GitHub outcome observer Stage 1 Part 2
(docs/proposals/active/github-outcome-observer-v0.md, KG 2026-11-14T20:58:05.769107+00:00).
Follows the 2026-11-19 §8.5 ruling (KG 2026-11-20T03:10:07.101348+00:00):
operator-credential gated, accepts only allowlisted ``github.*`` types, verifies
trusted ``pr_author`` provenance, refuses unknown ``agent_uuid`` subjects (404),
durably deduplicates by fingerprint in PostgreSQL (409), writes ``audit.events``,
and broadcasts on ``/ws/eisv`` without touching ``audit.outcome_events``.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from starlette.responses import JSONResponse

from src.broadcaster import broadcaster_instance
from src.dashboard_auth import dashboard_session_write_authorized
from src.http_routes import access
from src.logging_utils import get_logger

logger = get_logger(__name__)

EXTERNAL_GITHUB_EVENT_TYPES = frozenset(
    {
        "github.pr_opened",
        "github.pr_merged",
        "github.pr_closed",
        "github.pr_reopened",
        "github.ci_result",
        "github.review_record",
    }
)

EXTERNAL_EVENT_OPTIONAL_FIELDS = (
    "producer",
    "observed_at",
    "pr_author",
    "head_sha",
    "head_sha_at_observation",
    "merge_commit_sha",
    "source",
    "app",
    "workflow",
    "run_id",
    "attempt",
    "check_run_id",
    "conclusion",
    "failed_jobs",
    "key",
    "verdict",
    "findings",
    "disposed",
    "reviewer",
    "raw",
    "comment_id",
)

TRUSTED_GITHUB_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})

_MAX_BODY_BYTES = 65536
_MAX_FINGERPRINT_LEN = 256
_MAX_PRODUCER_LEN = 128
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_CANONICAL_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)


def configured_trusted_github_bot_ids() -> frozenset[int]:
    """Comma-separated APP_ID:BOT_ACTOR_ID or BOT_ACTOR_ID allowlist for external PR authors."""
    raw = os.getenv("UNITARES_EXTERNAL_TRUSTED_GITHUB_APPS", "")
    bot_ids: set[int] = set()
    for token in raw.split(","):
        item = token.strip()
        if not item:
            continue
        if ":" in item:
            app_id, _sep, actor_id = item.partition(":")
            if app_id.isdigit() and actor_id.isdigit():
                bot_ids.add(int(actor_id))
            else:
                logger.warning("Ignoring invalid UNITARES_EXTERNAL_TRUSTED_GITHUB_APPS entry")
        elif item.isdigit():
            bot_ids.add(int(item))
        else:
            logger.warning("Ignoring invalid UNITARES_EXTERNAL_TRUSTED_GITHUB_APPS entry")
    return frozenset(bot_ids)


def is_trusted_pr_author(pr_author: Any, trusted_bot_ids: frozenset[int]) -> bool:
    """Whether ``pr_author`` matches the configured GitHub association or trusted bot ID."""
    if not isinstance(pr_author, dict):
        return False
    association = pr_author.get("association")
    if isinstance(association, str) and association in TRUSTED_GITHUB_ASSOCIATIONS:
        return True
    actor_id = pr_author.get("id")
    return (
        isinstance(pr_author.get("type"), str)
        and pr_author.get("type") == "Bot"
        and type(actor_id) is int
        and actor_id in trusted_bot_ids
    )


def _valid_fingerprint(fp: Any) -> bool:
    if not isinstance(fp, str) or not (1 <= len(fp) <= _MAX_FINGERPRINT_LEN):
        return False
    return not any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in fp)


def _parse_observed_at(value: Any) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("observed_at must be an ISO-8601 timestamp string with timezone")
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("observed_at must include a timezone offset")
    return parsed.astimezone(timezone.utc)


async def http_record_external_event(request):
    """POST /v1/external/events — operator-gated ingestion for ``github.*`` events."""
    signals = access._build_http_session_signals(request)
    from src.mcp_handlers.identity.operator import is_operator_caller

    if not is_operator_caller(signals) and not dashboard_session_write_authorized(request):
        return JSONResponse(
            {
                "success": False,
                "error": "operator credential or passkey session with X-Unitares-Csrf: 1 required",
            },
            status_code=403,
        )

    raw = await request.body()
    if len(raw) > _MAX_BODY_BYTES:
        return JSONResponse(
            {"success": False, "error": "Payload exceeds 64 KiB limit"},
            status_code=413,
        )

    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return JSONResponse({"success": False, "error": "Invalid JSON body"}, status_code=400)

    if not isinstance(body, dict):
        return JSONResponse(
            {"success": False, "error": "Body must be a JSON object"}, status_code=400
        )

    event_type = body.get("type")
    if not isinstance(event_type, str) or event_type not in EXTERNAL_GITHUB_EVENT_TYPES:
        return JSONResponse(
            {
                "success": False,
                "error": f"type must be one of {sorted(EXTERNAL_GITHUB_EVENT_TYPES)}",
            },
            status_code=400,
        )

    raw_uuid = body.get("agent_uuid")
    if not isinstance(raw_uuid, str) or not _CANONICAL_UUID_RE.match(raw_uuid.strip().lower()):
        return JSONResponse(
            {"success": False, "error": "agent_uuid must be a canonical UUID"},
            status_code=400,
        )
    agent_uuid = str(uuid.UUID(raw_uuid.strip()))

    fingerprint = body.get("fingerprint")
    if not _valid_fingerprint(fingerprint):
        return JSONResponse(
            {
                "success": False,
                "error": f"fingerprint must be 1..{_MAX_FINGERPRINT_LEN} non-whitespace characters",
            },
            status_code=400,
        )

    repo = body.get("repo")
    if not isinstance(repo, str) or not _REPO_RE.match(repo):
        return JSONResponse(
            {"success": False, "error": "repo must be OWNER/NAME"}, status_code=400
        )

    pr_num = body.get("pr")
    if type(pr_num) is not int or pr_num <= 0:
        return JSONResponse(
            {"success": False, "error": "pr must be a positive integer"}, status_code=400
        )

    observed_at = body.get("observed_at")
    now = datetime.now(timezone.utc)
    if observed_at is not None:
        try:
            event_ts_input = _parse_observed_at(observed_at)
        except ValueError as exc:
            return JSONResponse({"success": False, "error": str(exc)}, status_code=400)
        if event_ts_input < now - timedelta(days=180):
            return JSONResponse(
                {"success": False, "error": "observed_at outside accepted retention window"},
                status_code=422,
            )
        if event_ts_input > now + timedelta(minutes=5):
            return JSONResponse(
                {"success": False, "error": "observed_at in the future"},
                status_code=422,
            )
    else:
        event_ts_input = now

    raw_producer = body.get("producer")
    if raw_producer is None:
        producer = "github_observer"
    elif not isinstance(raw_producer, str) or not (1 <= len(raw_producer.strip()) <= _MAX_PRODUCER_LEN):
        return JSONResponse(
            {
                "success": False,
                "error": f"producer must be 1..{_MAX_PRODUCER_LEN} characters",
            },
            status_code=400,
        )
    else:
        producer = raw_producer.strip()

    pr_author = body.get("pr_author")
    if not isinstance(pr_author, dict):
        return JSONResponse(
            {"success": False, "error": "pr_author must be an object"}, status_code=400
        )

    assoc = pr_author.get("association")
    actor_id = pr_author.get("id")
    actor_type = pr_author.get("type")
    if (
        (assoc is not None and not isinstance(assoc, str))
        or (actor_type is not None and not isinstance(actor_type, str))
        or (actor_id is not None and (type(actor_id) is not int or actor_id < 0))
    ):
        return JSONResponse(
            {"success": False, "error": "pr_author fields have invalid types"},
            status_code=400,
        )

    if not is_trusted_pr_author(pr_author, configured_trusted_github_bot_ids()):
        return JSONResponse(
            {"success": False, "error": "untrusted pr_author"}, status_code=403
        )

    try:
        from src.db import get_db

        db = get_db()
        identity = await db.get_identity(agent_uuid)
        if identity is None:
            return JSONResponse(
                {
                    "success": False,
                    "error": "unknown agent_uuid",
                    "agent_uuid": agent_uuid,
                },
                status_code=404,
            )

        clean_pr_author = {
            "association": pr_author.get("association"),
            "id": pr_author.get("id"),
            "type": pr_author.get("type"),
        }
        stored_payload: dict[str, Any] = {
            "fingerprint": fingerprint,
            "repo": repo,
            "pr": pr_num,
            "producer": producer,
            "pr_author": clean_pr_author,
        }
        for field in EXTERNAL_EVENT_OPTIONAL_FIELDS:
            if field in body and field not in stored_payload and body[field] is not None:
                stored_payload[field] = body[field]

        from src.audit_db import record_external_event_async

        inserted, event_id, event_ts = await record_external_event_async(
            fingerprint=fingerprint,
            event_type=event_type,
            agent_id=agent_uuid,
            ts=event_ts_input,
            producer=producer,
            payload=stored_payload,
        )
        if not inserted:
            return JSONResponse(
                {
                    "success": False,
                    "deduped": True,
                    "fingerprint": fingerprint,
                    "error": "duplicate fingerprint",
                },
                status_code=409,
            )

        frame_payload = {
            k: v for k, v in stored_payload.items() if k not in ("type", "agent_id")
        }
        frame_payload["event_id"] = event_id
        frame_payload["timestamp"] = event_ts.isoformat()
        await broadcaster_instance.broadcast_event(
            event_type,
            agent_id=agent_uuid,
            payload=frame_payload,
            persist=False,
        )
        return JSONResponse(
            {
                "success": True,
                "deduped": False,
                "event_id": event_id,
                "event_type": event_type,
                "agent_uuid": agent_uuid,
                "fingerprint": fingerprint,
                "ts": event_ts.isoformat(),
            },
            status_code=201,
        )
    except Exception as exc:
        logger.error("Failed to record external event: %s", exc)
        return JSONResponse(
            {"success": False, "error": "internal error"}, status_code=500
        )
