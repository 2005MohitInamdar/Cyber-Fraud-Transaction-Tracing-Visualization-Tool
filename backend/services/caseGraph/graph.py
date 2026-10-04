"""Build the browser-safe transaction-flow graph for a persisted case."""

import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from db.connection import get_connection, get_redis

_TTL_SECONDS = 60 * 60 * 24


def _cache_key(upload_id: str) -> str:
    return f"case:{upload_id}:graph"


def _as_list(value: Any) -> list[Any]:
    """Decode JSON list columns consistently across MySQL driver versions."""
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8")
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return []
    return value if isinstance(value, list) else []


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def _number(value: Any) -> float | None:
    value = _json_value(value)
    return float(value) if value is not None else None


def _boolean(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes"}
    return bool(value)


def _empty_summary() -> dict[str, Any]:
    return {
        "ackNo": None,
        "status": None,
        "baseDebitTotal": None,
        "reportedFraudTotal": None,
        "holdTotal": None,
        "reportedLienTotal": None,
        "holdsMatchLien": False,
        "nodeCount": 0,
        "edgeCount": 0,
        "layers": [],
        "unmatchedHoldCount": 0,
    }


def build_graph_payload(
    case_row: dict[str, Any] | None,
    node_rows: list[dict[str, Any]],
    edge_rows: list[dict[str, Any]],
    hold_rows: list[dict[str, Any]],
    hold_link_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """Return a deterministic, JSON-ready graph without doing database I/O."""
    if case_row is None:
        return {"hasGraph": False, "summary": _empty_summary(), "nodes": [], "edges": []}

    holds = {row.get("hold_id"): row for row in hold_rows}
    holds_by_node: dict[str, list[dict[str, Any]]] = {}
    linked_hold_ids: set[str] = set()
    for link in hold_link_rows:
        hold_id = link.get("hold_id")
        node_id = link.get("node_id")
        hold = holds.get(hold_id)
        if not hold or not node_id:
            continue
        linked_hold_ids.add(hold_id)
        holds_by_node.setdefault(node_id, []).append(
            {
                "holdId": hold_id,
                "amount": _number(link.get("amount") if link.get("amount") is not None else hold.get("hold_amount")),
                "date": _json_value(hold.get("hold_date")),
                "actionTakenBy": hold.get("action_taken_by") or "",
                "matchRule": link.get("match_rule") or "",
                "confidence": _number(link.get("confidence")),
            }
        )

    node_ids = {row.get("node_id") for row in node_rows if row.get("node_id")}
    valid_edges = [
        row for row in edge_rows
        if row.get("from_node") in node_ids and row.get("to_node") in node_ids
    ]
    incoming = {row["to_node"] for row in valid_edges}
    outgoing = {row["from_node"] for row in valid_edges}

    nodes: list[dict[str, Any]] = []
    for row in node_rows:
        node_id = row.get("node_id")
        if not node_id:
            continue
        layer = int(row.get("layer") or 0)
        if layer == 0:
            role = "victim"
        elif node_id not in incoming and node_id not in outgoing:
            role = "isolated"
        elif node_id in incoming and node_id not in outgoing:
            role = "endOfTrail"
        else:
            role = "intermediate"
        nodes.append(
            {
                "id": node_id,
                "layer": layer,
                "bank": row.get("bank") or "",
                "actionTakenBy": row.get("action_taken_by") or "",
                "accountNo": row.get("account_no") or "",
                "utr": row.get("utr") or "",
                "txAmount": _number(row.get("tx_amount")),
                "disputedAmount": _number(row.get("disputed_amount")),
                "amountEstimated": _boolean(row.get("amount_estimated")),
                "frozenAmount": _number(row.get("frozen_amount")) or 0.0,
                "unaccountedAmount": _number(row.get("unaccounted_amount")),
                "embeddedIds": _as_list(row.get("embedded_ids")),
                "rootIds": _as_list(row.get("root_ids")),
                "remarks": row.get("remarks") or "",
                "role": role,
                "holds": sorted(holds_by_node.get(node_id, []), key=lambda hold: hold["holdId"]),
            }
        )

    edges = [
        {
            "source": row["from_node"],
            "target": row["to_node"],
            "matchRule": row.get("match_rule") or "",
            "confidence": _number(row.get("confidence")),
            "amountPassed": _number(row.get("amount_passed")),
            "ambiguous": _boolean(row.get("ambiguous")),
            "merged": _boolean(row.get("merged")),
            "amountEstimated": _boolean(row.get("amount_estimated")),
        }
        for row in valid_edges
    ]
    nodes.sort(key=lambda node: (node["layer"], node["id"]))
    edges.sort(key=lambda edge: (edge["source"], edge["target"]))
    layers = sorted({node["layer"] for node in nodes})

    return {
        "hasGraph": True,
        "summary": {
            "ackNo": case_row.get("ack_no"),
            "status": case_row.get("status"),
            "baseDebitTotal": _number(case_row.get("base_debit_total")),
            "reportedFraudTotal": _number(case_row.get("reported_fraud_total")),
            "holdTotal": _number(case_row.get("hold_total")),
            "reportedLienTotal": _number(case_row.get("reported_lien_total")),
            "holdsMatchLien": _boolean(case_row.get("holds_match_lien")),
            "nodeCount": len(nodes),
            "edgeCount": len(edges),
            "layers": layers,
            "unmatchedHoldCount": int(case_row.get("unmatched_hold_count") or (len(holds) - len(linked_hold_ids))),
        },
        "nodes": nodes,
        "edges": edges,
    }


def get_case_graph(user_id: str, upload_id: str) -> dict[str, Any]:
    """Load one owned case graph, checking ownership before the shared cache."""
    with get_connection() as conn:
        with conn.cursor(dictionary=True) as cur:
            cur.execute(
                """
                SELECT 1 FROM fraud_case_uploads
                WHERE upload_id = %s AND supabase_user_id = %s LIMIT 1
                """,
                (upload_id, user_id),
            )
            if cur.fetchone() is None:
                raise PermissionError("You do not have access to this case.")

            cached = get_redis().get(_cache_key(upload_id))
            if cached:
                return json.loads(cached)

            cur.execute(
                """
                SELECT id, ack_no, status, base_debit_total, reported_fraud_total,
                       hold_total, reported_lien_total, holds_match_lien
                FROM cases
                WHERE upload_id = %s AND supabase_user_id = %s
                LIMIT 1
                """,
                (upload_id, user_id),
            )
            case_row = cur.fetchone()
            if case_row is None:
                result = build_graph_payload(None, [], [], [], [])
            else:
                case_id = case_row.pop("id")
                cur.execute("SELECT node_id, layer, bank, action_taken_by, account_no, utr, tx_amount, disputed_amount, amount_estimated, frozen_amount, unaccounted_amount, embedded_ids, root_ids, remarks FROM nodes WHERE case_id = %s", (case_id,))
                nodes = cur.fetchall()
                cur.execute("SELECT from_node, to_node, match_rule, confidence, amount_passed, ambiguous, merged, amount_estimated FROM edges WHERE case_id = %s", (case_id,))
                edges = cur.fetchall()
                cur.execute("SELECT hold_id, account_no, hold_amount, hold_date, action_taken_by, remarks FROM holds WHERE case_id = %s", (case_id,))
                holds = cur.fetchall()
                cur.execute("SELECT hold_id, node_id, match_rule, confidence, amount FROM hold_links WHERE case_id = %s", (case_id,))
                hold_links = cur.fetchall()
                cur.execute("SELECT COUNT(*) AS unmatched_hold_count FROM holds h LEFT JOIN hold_links hl ON hl.case_id = h.case_id AND hl.hold_id = h.hold_id WHERE h.case_id = %s AND hl.hold_id IS NULL", (case_id,))
                case_row["unmatched_hold_count"] = cur.fetchone()["unmatched_hold_count"]
                result = build_graph_payload(case_row, nodes, edges, holds, hold_links)

    get_redis().set(_cache_key(upload_id), json.dumps(result), ex=_TTL_SECONDS)
    return result
