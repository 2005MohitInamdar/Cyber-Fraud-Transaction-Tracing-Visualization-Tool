"""Build the browser-safe transaction-flow graph for a persisted case."""

import json
import re
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from db.connection import get_connection, get_redis

_TTL_SECONDS = 60 * 60 * 24
UNACCOUNTED_TOLERANCE = 1.00
LOW_CONFIDENCE = 0.75
WITHDRAWAL_KEYWORDS = ("atm", "withdraw", "cash")
_SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}


def _cache_key(upload_id: str) -> str:
    return f"case:{upload_id}:graph:v2"


def _as_list(value: Any) -> list[Any]:
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
    return value.strip().lower() in {"1", "true", "yes"} if isinstance(value, str) else bool(value)


def _bank_tokens(value: Any) -> frozenset[str]:
    """Exact full-bank matching after lowercasing and stripping punctuation."""
    return frozenset(re.findall(r"[\w]+", str(value or "").lower()))


def _withdrawal_note(value: Any) -> bool:
    remarks = str(value or "").lower()
    return any(re.search(rf"\b{re.escape(keyword)}(?:n|al)?\b", remarks) for keyword in WITHDRAWAL_KEYWORDS)


def _reason(code: str, message: str, severity: str) -> dict[str, str]:
    return {"code": code, "message": message, "severity": severity}


def _empty_summary() -> dict[str, Any]:
    return {"ackNo": None, "status": None, "baseDebitTotal": None, "reportedFraudTotal": None,
            "holdTotal": None, "reportedLienTotal": None, "holdsMatchLien": False, "nodeCount": 0,
            "edgeCount": 0, "layers": [], "unmatchedHoldCount": 0, "incompleteCount": 0,
            "orphanHoldCount": 0}


def build_graph_payload(
    case_row: dict[str, Any] | None,
    node_rows: list[dict[str, Any]],
    edge_rows: list[dict[str, Any]],
    hold_rows: list[dict[str, Any]],
    hold_link_rows: list[dict[str, Any]],
    pending_rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return a deterministic, JSON-ready graph without database I/O."""
    if case_row is None:
        return {"hasGraph": False, "summary": _empty_summary(), "nodes": [], "edges": [], "incomplete": [], "orphanHolds": []}

    pending_banks = {_bank_tokens(row.get("bank")) for row in (pending_rows or []) if _bank_tokens(row.get("bank"))}
    node_ids = {row.get("node_id") for row in node_rows if row.get("node_id")}
    holds = {row.get("hold_id"): row for row in hold_rows if row.get("hold_id")}
    holds_by_node: dict[str, list[dict[str, Any]]] = {}
    linked_hold_ids: set[str] = set()
    for link in hold_link_rows:
        hold_id, node_id = link.get("hold_id"), link.get("node_id")
        hold = holds.get(hold_id)
        if not hold or node_id not in node_ids:
            continue
        linked_hold_ids.add(hold_id)
        holds_by_node.setdefault(node_id, []).append({
            "holdId": hold_id,
            "amount": _number(link.get("amount") if link.get("amount") is not None else hold.get("hold_amount")),
            "date": _json_value(hold.get("hold_date")),
            "actionTakenBy": hold.get("action_taken_by") or "",
            "matchRule": link.get("match_rule") or "",
            "confidence": _number(link.get("confidence")),
        })

    valid_edges = [row for row in edge_rows if row.get("from_node") in node_ids and row.get("to_node") in node_ids]
    incoming_by_node: dict[str, list[dict[str, Any]]] = {node_id: [] for node_id in node_ids}
    outgoing_by_node: dict[str, list[dict[str, Any]]] = {node_id: [] for node_id in node_ids}
    for edge in valid_edges:
        incoming_by_node[edge["to_node"]].append(edge)
        outgoing_by_node[edge["from_node"]].append(edge)

    nodes: list[dict[str, Any]] = []
    incomplete: list[dict[str, Any]] = []
    for row in node_rows:
        node_id = row.get("node_id")
        if not node_id:
            continue
        layer = int(row.get("layer") or 0)
        incoming, outgoing = incoming_by_node[node_id], outgoing_by_node[node_id]
        linked_holds = sorted(holds_by_node.get(node_id, []), key=lambda hold: hold["holdId"])
        tx_amount, disputed_amount = _number(row.get("tx_amount")), _number(row.get("disputed_amount"))
        amount = disputed_amount if disputed_amount is not None else (tx_amount or 0.0)
        frozen_amount = _number(row.get("frozen_amount")) or 0.0
        # Support older persisted graphs that have a linked hold but did not copy it to frozen_amount.
        frozen = max(frozen_amount, sum((hold["amount"] or 0.0) for hold in linked_holds))
        passed_out = sum((_number(edge.get("amount_passed")) or 0.0) for edge in outgoing)
        remaining = amount - passed_out - frozen
        has_hold = frozen > 0 or bool(linked_holds)
        bank_tokens = _bank_tokens(row.get("bank"))
        has_pending = bool(bank_tokens) and bank_tokens in pending_banks
        has_withdrawal_note = _withdrawal_note(row.get("remarks"))
        resolved_by = "hold" if has_hold else ("pending" if has_pending and not outgoing else ("withdrawal_note" if has_withdrawal_note and not outgoing else None))

        reasons: list[dict[str, str]] = []
        end_without_status = layer > 0 and bool(incoming) and not outgoing and not (has_hold or has_pending or has_withdrawal_note)
        if end_without_status:
            reasons.append(_reason("END_NO_STATUS", "Money reached this account but nothing shows where it went (no hold, pending record or withdrawal note).", "high"))
        if remaining > UNACCOUNTED_TOLERANCE and not end_without_status and not (has_pending or has_withdrawal_note):
            reasons.append(_reason("UNACCOUNTED_AMOUNT", f"₹{remaining:,.2f} is unaccounted for after matched transfers and frozen funds.", "medium"))
        if layer > 0 and not incoming:
            reasons.append(_reason("NO_INCOMING_LINK", "No earlier transaction could be matched to this one.", "high"))
        if layer == 0 and not outgoing:
            reasons.append(_reason("VICTIM_UNTRACED", "This victim debit has no matched onward transaction.", "high"))
        if _boolean(row.get("amount_estimated")):
            reasons.append(_reason("AMOUNT_ESTIMATED", "The transaction amount was estimated from incomplete source data.", "low"))
        if any(
            _boolean(edge.get("ambiguous"))
            or (_number(edge.get("confidence")) is not None and _number(edge.get("confidence")) < LOW_CONFIDENCE)
            for edge in incoming
        ):
            reasons.append(_reason("LOW_CONFIDENCE_LINK", "At least one earlier transaction link is ambiguous or low confidence.", "low"))

        severity = min((reason["severity"] for reason in reasons), key=lambda value: _SEVERITY_ORDER[value], default=None)
        completeness = {"status": "incomplete" if reasons else "complete", "reasons": [{"code": r["code"], "message": r["message"]} for r in reasons], "resolvedBy": resolved_by}
        role = "victim" if layer == 0 else ("isolated" if not incoming and not outgoing else ("endOfTrail" if incoming and not outgoing else "intermediate"))
        node = {"id": node_id, "layer": layer, "bank": row.get("bank") or "", "actionTakenBy": row.get("action_taken_by") or "", "accountNo": row.get("account_no") or "", "utr": row.get("utr") or "", "txAmount": tx_amount, "disputedAmount": disputed_amount, "amountEstimated": _boolean(row.get("amount_estimated")), "frozenAmount": frozen_amount, "unaccountedAmount": _number(row.get("unaccounted_amount")), "embeddedIds": _as_list(row.get("embedded_ids")), "rootIds": _as_list(row.get("root_ids")), "remarks": row.get("remarks") or "", "role": role, "holds": linked_holds, "completeness": completeness}
        nodes.append(node)
        if reasons:
            incomplete.append({"kind": "node", "nodeId": node_id, "layer": layer, "bank": node["bank"], "accountNo": node["accountNo"], "utr": node["utr"], "disputedAmount": disputed_amount, "txAmount": tx_amount, "unaccountedAmount": max(remaining, 0.0), "severity": severity, "reasons": completeness["reasons"]})

    edges = [{"source": row["from_node"], "target": row["to_node"], "matchRule": row.get("match_rule") or "", "confidence": _number(row.get("confidence")), "amountPassed": _number(row.get("amount_passed")), "ambiguous": _boolean(row.get("ambiguous")), "merged": _boolean(row.get("merged")), "amountEstimated": _boolean(row.get("amount_estimated"))} for row in valid_edges]
    orphan_holds = [{"kind": "hold", "holdId": hold_id, "accountNo": hold.get("account_no") or "", "amount": _number(hold.get("hold_amount")), "date": _json_value(hold.get("hold_date")), "actionTakenBy": hold.get("action_taken_by") or "", "remarks": hold.get("remarks") or "", "severity": "medium", "reasons": [{"code": "HOLD_WITHOUT_TRAIL", "message": "This frozen/held account could not be matched to a transaction trail."}]} for hold_id, hold in holds.items() if hold_id not in linked_hold_ids]
    nodes.sort(key=lambda node: (node["layer"], node["id"])); edges.sort(key=lambda edge: (edge["source"], edge["target"])); incomplete.sort(key=lambda item: (_SEVERITY_ORDER[item["severity"]], item["layer"], item["nodeId"])); orphan_holds.sort(key=lambda item: item["holdId"])
    layers = sorted({node["layer"] for node in nodes})
    return {"hasGraph": True, "summary": {"ackNo": case_row.get("ack_no"), "status": case_row.get("status"), "baseDebitTotal": _number(case_row.get("base_debit_total")), "reportedFraudTotal": _number(case_row.get("reported_fraud_total")), "holdTotal": _number(case_row.get("hold_total")), "reportedLienTotal": _number(case_row.get("reported_lien_total")), "holdsMatchLien": _boolean(case_row.get("holds_match_lien")), "nodeCount": len(nodes), "edgeCount": len(edges), "layers": layers, "unmatchedHoldCount": len(orphan_holds), "incompleteCount": len(incomplete), "orphanHoldCount": len(orphan_holds)}, "nodes": nodes, "edges": edges, "incomplete": incomplete, "orphanHolds": orphan_holds}


def get_case_graph(user_id: str, upload_id: str) -> dict[str, Any]:
    """Load one owned case graph, checking ownership before the shared cache."""
    with get_connection() as conn:
        with conn.cursor(dictionary=True) as cur:
            cur.execute("SELECT 1 FROM fraud_case_uploads WHERE upload_id = %s AND supabase_user_id = %s LIMIT 1", (upload_id, user_id))
            if cur.fetchone() is None:
                raise PermissionError("You do not have access to this case.")
            cached = get_redis().get(_cache_key(upload_id))
            if cached:
                return json.loads(cached)
            cur.execute("SELECT id, ack_no, status, base_debit_total, reported_fraud_total, hold_total, reported_lien_total, holds_match_lien FROM cases WHERE upload_id = %s AND supabase_user_id = %s LIMIT 1", (upload_id, user_id))
            case_row = cur.fetchone()
            if case_row is None:
                result = build_graph_payload(None, [], [], [], [])
            else:
                case_id = case_row.pop("id")
                cur.execute("SELECT node_id, layer, bank, action_taken_by, account_no, utr, tx_amount, disputed_amount, amount_estimated, frozen_amount, unaccounted_amount, embedded_ids, root_ids, remarks FROM nodes WHERE case_id = %s", (case_id,)); nodes = cur.fetchall()
                cur.execute("SELECT from_node, to_node, match_rule, confidence, amount_passed, ambiguous, merged, amount_estimated FROM edges WHERE case_id = %s", (case_id,)); edges = cur.fetchall()
                cur.execute("SELECT hold_id, account_no, hold_amount, hold_date, action_taken_by, remarks FROM holds WHERE case_id = %s", (case_id,)); holds = cur.fetchall()
                cur.execute("SELECT hold_id, node_id, match_rule, confidence, amount FROM hold_links WHERE case_id = %s", (case_id,)); hold_links = cur.fetchall()
                cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = DATABASE() AND table_name = 'pending_transactions'")
                pending_rows: list[dict[str, Any]] = []
                if cur.fetchone():
                    cur.execute("SELECT bank_fi AS bank, no_of_transactions_pending, amount_pending, pending_from FROM pending_transactions WHERE upload_id = %s AND supabase_user_id = %s", (upload_id, user_id)); pending_rows = cur.fetchall()
                result = build_graph_payload(case_row, nodes, edges, holds, hold_links, pending_rows)
    get_redis().set(_cache_key(upload_id), json.dumps(result), ex=_TTL_SECONDS)
    return result
