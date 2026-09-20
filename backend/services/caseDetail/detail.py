"""Authenticated, cached case-detail retrieval."""

import json
from datetime import date, datetime
from decimal import Decimal

from db.connection import get_connection, get_redis

_TTL_SECONDS = 60 * 60 * 24

_TABLES = {
    "amountSummary": "amount_summary",
    "complaintTransactions": "complaint_transactions",
    "failedTransactions": "failed_transactions",
    "holdAccounts": "hold_accounts",
    "lienTransactions": "lien_transactions",
    "noActionReferences": "no_action_references",
    "pendingTransactions": "pending_transactions",
}


def _cache_key(upload_id: str) -> str:
    return f"case:{upload_id}:detail"


def invalidate_case_detail(upload_id: str) -> None:
    """Remove a cached snapshot after case data changes."""
    get_redis().delete(_cache_key(upload_id))


def _camel_case(name: str) -> str:
    head, *tail = name.split("_")
    return head + "".join(part.capitalize() for part in tail)


def _json_value(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value


def _camel_row(row: dict | None) -> dict:
    return {
        _camel_case(key): _json_value(value)
        for key, value in (row or {}).items()
    }


def get_case_detail(user_id: str, upload_id: str) -> dict:
    """Return all case data only if ``upload_id`` belongs to ``user_id``."""
    with get_connection() as conn:
        with conn.cursor(dictionary=True) as cur:
            cur.execute(
                """
                SELECT inspector_name, inspector_rank, inspector_branch, created_at
                FROM fraud_case_uploads
                WHERE upload_id = %s AND supabase_user_id = %s
                LIMIT 1
                """,
                (upload_id, user_id),
            )
            lead = cur.fetchone()
            if lead is None:
                raise PermissionError("You do not have access to this case.")

            # Ownership is checked before this cache read; cache keys are not
            # user-specific because upload IDs are globally unique.
            cached = get_redis().get(_cache_key(upload_id))
            if cached:
                return json.loads(cached)

            cur.execute(
                """
                SELECT file_name, file_size, content_type, status, file_path,
                       created_at, completed_at
                FROM file_upload_sessions
                WHERE upload_id = %s AND supabase_user_id = %s
                LIMIT 1
                """,
                (upload_id, user_id),
            )
            upload = cur.fetchone() or {}

            tables = {}
            for response_key, table in _TABLES.items():
                cur.execute(
                    f"SELECT * FROM `{table}` "
                    "WHERE upload_id = %s AND supabase_user_id = %s "
                    "ORDER BY s_no ASC",
                    (upload_id, user_id),
                )
                tables[response_key] = [_camel_row(row) for row in cur.fetchall()]

            cur.execute(
                """
                SELECT * FROM complaint_meta
                WHERE upload_id = %s AND supabase_user_id = %s
                ORDER BY complaint_meta_id DESC
                LIMIT 1
                """,
                (upload_id, user_id),
            )
            tables["complaintMeta"] = _camel_row(cur.fetchone())

    result = {
        "uploadId": upload_id,
        "leadOfficer": {
            "inspectorName": lead["inspector_name"],
            "inspectorRank": lead["inspector_rank"],
            "inspectorBranch": lead["inspector_branch"],
        },
        "upload": {
            "fileName": upload.get("file_name"),
            "fileSize": upload.get("file_size"),
            "contentType": upload.get("content_type"),
            "status": upload.get("status"),
            "filePath": upload.get("file_path"),
            "createdAt": _json_value(upload.get("created_at")),
            "completedAt": _json_value(upload.get("completed_at")),
        },
        "tables": tables,
    }
    get_redis().set(_cache_key(upload_id), json.dumps(result), ex=_TTL_SECONDS)
    return result
