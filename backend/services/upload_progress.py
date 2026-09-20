"""Short-lived, user-scoped progress events for a case upload."""

import json
from datetime import datetime, timezone

from db.connection import get_connection, get_redis

_TTL_SECONDS = 60 * 60 * 24


def _key(upload_id: str) -> str:
    return f"upload:{upload_id}:progress"


def publish(upload_id: str, message: str, state: str = "processing") -> None:
    """Append an event that the frontend can poll while work is in progress."""
    event = {
        "message": message,
        "state": state,
        "at": datetime.now(timezone.utc).isoformat(),
    }
    redis = get_redis()
    redis.rpush(_key(upload_id), json.dumps(event))
    redis.expire(_key(upload_id), _TTL_SECONDS)


def get_events_for_user(upload_id: str, user_id: str) -> list[dict]:
    """Return events only when the requested upload belongs to ``user_id``."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT 1 FROM file_upload_sessions
                WHERE upload_id = %s AND supabase_user_id = %s
                LIMIT 1
                """,
                (upload_id, user_id),
            )
            if cur.fetchone() is None:
                raise PermissionError("You do not have access to this upload.")

    redis = get_redis()
    return [json.loads(raw) for raw in redis.lrange(_key(upload_id), 0, -1)]
