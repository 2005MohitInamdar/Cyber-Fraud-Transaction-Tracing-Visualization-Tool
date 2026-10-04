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

# Columns of the transaction-flow tables that need special handling.
_JSON_FIELDS = {"embedded_ids", "root_ids", "checks"}
_BOOL_FIELDS = {"amount_estimated", "ambiguous", "merged", "holds_match_lien"}

# Derived "no flow" lists (views over nodes / edges / hold_links).
_FLOW_VIEWS = {
    "unlinkedNodeIds": "v_unlinked_rows",
    "untracedBaseNodeIds": "v_untraced_base",
    "endOfTrailNodeIds": "v_end_of_trail",
    "noFlowNodeIds": "v_no_flow",
}


def _cache_key(upload_id: str) -> str:
    return f"case:{upload_id}:detail"


def invalidate_case_detail(upload_id: str) -> None:
    """Remove a cached snapshot after case data changes."""
    get_redis().delete(_cache_key(upload_id), f"case:{upload_id}:graph")


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


def _loads(value):
    """JSON columns come back as str or bytes depending on the driver."""
    if value is None or value == "":
        return None
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8")
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _flow_row(row: dict | None) -> dict:
    out = {}
    for key, value in (row or {}).items():
        if key in _JSON_FIELDS:
            out[_camel_case(key)] = _loads(value)
        elif key in _BOOL_FIELDS:
            out[_camel_case(key)] = None if value is None else bool(value)
        else:
            out[_camel_case(key)] = _json_value(value)
    return out


def _get_flow(cur, user_id: str, upload_id: str):
    """The transaction-flow graph for this upload, or None if it has not been built."""
    cur.execute(
        """
        SELECT id, ack_no, status, base_debit_total, reported_fraud_total,
               hold_total, reported_lien_total, holds_match_lien, checks, created_at
        FROM cases
        WHERE upload_id = %s AND supabase_user_id = %s
        LIMIT 1
        """,
        (upload_id, user_id),
    )
    case = cur.fetchone()
    if case is None:
        return None
    case_id = case.pop("id")          # internal key, not sent to the browser

    cur.execute(
        """
        SELECT node_id, layer, bank, action_taken_by, account_no, utr, tx_amount,
               disputed_amount, amount_estimated, frozen_amount, unaccounted_amount,
               embedded_ids, root_ids, remarks
        FROM nodes WHERE case_id = %s ORDER BY layer, node_id
        """,
        (case_id,),
    )
    nodes = [_flow_row(row) for row in cur.fetchall()]

    cur.execute(
        """
        SELECT from_node, to_node, match_rule, confidence, amount_passed,
               ambiguous, merged, amount_estimated
        FROM edges WHERE case_id = %s ORDER BY id
        """,
        (case_id,),
    )
    edges = [_flow_row(row) for row in cur.fetchall()]

    cur.execute(
        """
        SELECT h.hold_id, h.account_no, h.hold_amount, h.hold_date, h.action_taken_by,
               h.remarks, l.node_id, l.match_rule, l.confidence
        FROM holds h
        LEFT JOIN hold_links l ON l.case_id = h.case_id AND l.hold_id = h.hold_id
        WHERE h.case_id = %s
        ORDER BY h.hold_date, h.hold_id
        """,
        (case_id,),
    )
    holds = [_flow_row(row) for row in cur.fetchall()]

    lists = {}
    for key, view in _FLOW_VIEWS.items():
        cur.execute(f"SELECT node_id FROM `{view}` WHERE case_id = %s ORDER BY node_id", (case_id,))
        lists[key] = [row["node_id"] for row in cur.fetchall()]

    return {
        "case": _flow_row(case),
        "nodes": nodes,
        "edges": edges,
        "holds": holds,
        **lists,
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

            # Legacy extraction tables are optional in the current schema.  Keep
            # their response shape for the raw-data toggle, but do not prevent a
            # case (and its graph) from loading when an older table was removed.
            cur.execute(
                """
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = DATABASE()
                  AND table_name IN (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (*_TABLES.values(), "complaint_meta"),
            )
            available_tables = {row["table_name"] for row in cur.fetchall()}

            tables = {response_key: [] for response_key in _TABLES}
            for response_key, table in _TABLES.items():
                if table not in available_tables:
                    continue
                cur.execute(
                    f"SELECT * FROM `{table}` "
                    "WHERE upload_id = %s AND supabase_user_id = %s "
                    "ORDER BY s_no ASC",
                    (upload_id, user_id),
                )
                tables[response_key] = [_camel_row(row) for row in cur.fetchall()]

            tables["complaintMeta"] = {}
            if "complaint_meta" in available_tables:
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

            flow = _get_flow(cur, user_id, upload_id)

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
        "flow": flow,
    }
    get_redis().set(_cache_key(upload_id), json.dumps(result), ex=_TTL_SECONDS)
    return result
