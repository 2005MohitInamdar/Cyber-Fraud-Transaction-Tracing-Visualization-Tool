"""
Read missing-data items for a case directly from the DB.

Returns a payload shaped like get_case_graph()'s output, so it can be passed
straight into collect_requisition_items().
"""

from __future__ import annotations

from typing import Any

from db.connection import get_connection

# One query flags every node. EXISTS subqueries mirror your v_* views.
_NODES_SQL = """
SELECT
    n.node_id, n.layer, n.bank, n.account_no, n.utr,
    n.tx_amount, n.disputed_amount, n.unaccounted_amount,
    n.amount_estimated,
    EXISTS (SELECT 1 FROM edges e
            WHERE e.case_id = n.case_id AND e.to_node = n.node_id)   AS has_parent,
    EXISTS (SELECT 1 FROM edges e
            WHERE e.case_id = n.case_id AND e.from_node = n.node_id) AS has_child,
    EXISTS (SELECT 1 FROM hold_links h
            WHERE h.case_id = n.case_id AND h.node_id = n.node_id)   AS has_hold
FROM nodes n
JOIN cases c ON c.id = n.case_id
WHERE c.upload_id = %s AND c.supabase_user_id = %s
ORDER BY n.layer, n.account_no, n.utr
"""

_ORPHAN_HOLDS_SQL = """
SELECT h.hold_id, h.account_no, h.hold_amount, h.hold_date, h.action_taken_by
FROM holds h
JOIN cases c ON c.id = h.case_id
WHERE c.upload_id = %s AND c.supabase_user_id = %s
  AND NOT EXISTS (SELECT 1 FROM hold_links hl
                  WHERE hl.case_id = h.case_id AND hl.hold_id = h.hold_id)
ORDER BY h.hold_date, h.account_no
"""


def _reasons_for_node(row: dict[str, Any]) -> list[dict[str, str]]:
    codes: list[str] = []
    layer = row["layer"]

    if layer == 0:
        # Victim debit that matched nothing (internal only, never emailed)
        if not row["has_child"]:
            codes.append("VICTIM_UNTRACED")
    else:
        if not row["has_parent"]:
            codes.append("NO_INCOMING_LINK")
        # End of trail: has a parent, passes nothing on, no hold recorded
        elif not row["has_child"] and not row["has_hold"]:
            codes.append("END_NO_STATUS")
        if (row["unaccounted_amount"] or 0) > 0:
            codes.append("UNACCOUNTED_AMOUNT")

    if row["amount_estimated"]:
        codes.append("AMOUNT_ESTIMATED")

    return [{"code": c} for c in codes]


def get_missing_data(user_id: str, upload_id: str) -> dict[str, Any]:
    """
    Fetch incomplete nodes and orphan holds for one case.

    Scoped by BOTH upload_id and supabase_user_id, so a user can never read
    another user's case.
    """
    incomplete: list[dict[str, Any]] = []
    orphan_holds: list[dict[str, Any]] = []

    with get_connection() as conn:
        with conn.cursor(dictionary=True) as cur:
            cur.execute(_NODES_SQL, (upload_id, user_id))
            for row in cur.fetchall():
                reasons = _reasons_for_node(row)
                if not reasons:
                    continue  # complete node, nothing missing
                incomplete.append({
                    "nodeId":             row["node_id"],
                    "layer":              row["layer"],
                    "bank":               row["bank"] or "",
                    "accountNo":          row["account_no"] or "",
                    "utr":                row["utr"] or "",
                    "txAmount":           row["tx_amount"],
                    "disputedAmount":     row["disputed_amount"],
                    "unaccountedAmount":  row["unaccounted_amount"],
                    "reasons":            reasons,
                })

            cur.execute(_ORPHAN_HOLDS_SQL, (upload_id, user_id))
            for row in cur.fetchall():
                orphan_holds.append({
                    "holdId":        row["hold_id"],
                    "accountNo":     row["account_no"] or "",
                    "amount":        row["hold_amount"],
                    "date":          row["hold_date"],
                    "actionTakenBy": row["action_taken_by"] or "",
                    "reasons":       [{"code": "HOLD_WITHOUT_TRAIL"}],
                })

    return {"incomplete": incomplete, "orphanHolds": orphan_holds}


