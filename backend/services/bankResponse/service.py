"""Persistence helpers for reviewable bank-response uploads."""
from __future__ import annotations

import json
from typing import Any

from db.connection import get_connection
from services.bankResponse.parse import mark_duplicates, row_key
from services.pdf_extraction.transaction_flow import _clean_id, FlowConfig
from services.email.bank_names import normalize_bank_name
from services.caseGraph.graph import get_case_graph


def _require_upload_owner(cur, user_id: str, upload_id: str) -> None:
    cur.execute("SELECT 1 FROM fraud_case_uploads WHERE upload_id=%s AND supabase_user_id=%s LIMIT 1", (upload_id, user_id))
    if cur.fetchone() is None:
        raise PermissionError("You do not have access to this case.")


def _owned_response(cur, user_id: str, upload_id: str, response_id: int) -> dict[str, Any]:
    cur.execute("SELECT * FROM bank_responses WHERE id=%s AND upload_id=%s AND supabase_user_id=%s LIMIT 1", (response_id, upload_id, user_id))
    response = cur.fetchone()
    if response is None:
        raise PermissionError("You do not have access to this bank response.")
    return response


def create_parsed_response(user_id: str, upload_id: str, file_hash: str, file_path: str, requisition_id: int | None, parsed: dict[str, Any]) -> dict[str, Any]:
    with get_connection() as conn:
        with conn.cursor(dictionary=True) as cur:
            _require_upload_owner(cur, user_id, upload_id)
            if requisition_id is not None:
                cur.execute("SELECT id FROM requisitions WHERE id=%s AND upload_id=%s AND supabase_user_id=%s", (requisition_id, upload_id, user_id))
                if cur.fetchone() is None:
                    raise PermissionError("That requisition does not belong to this case.")
            cur.execute("SELECT id FROM bank_responses WHERE upload_id=%s AND file_hash=%s LIMIT 1", (upload_id, file_hash))
            duplicate = cur.fetchone()
            if duplicate:
                raise FileExistsError(str(duplicate["id"]))
            cur.execute("SELECT node_id, utr FROM nodes WHERE case_id=(SELECT id FROM cases WHERE upload_id=%s AND supabase_user_id=%s ORDER BY revision DESC LIMIT 1)", (upload_id, user_id))
            existing_utrs = {_clean_id(row.get("utr"), FlowConfig()) for row in cur.fetchall() if row.get("utr")}
            cur.execute("SELECT row_key FROM bank_response_rows r JOIN bank_responses b ON b.id=r.response_id WHERE b.upload_id=%s AND b.status='applied'", (upload_id,))
            existing_keys = {row["row_key"] for row in cur.fetchall()}
            rows = [("lien_transactions", row) for row in parsed.get("lienRows", [])] + [("hold_accounts", row) for row in parsed.get("holdRows", [])]
            marked = mark_duplicates(rows, existing_keys, existing_utrs)
            cur.execute("INSERT INTO bank_responses (upload_id,supabase_user_id,requisition_id,file_hash,file_path,status,parse_report) VALUES (%s,%s,%s,%s,%s,'parsed',%s)", (upload_id, user_id, requisition_id, file_hash, file_path, json.dumps({k: parsed.get(k) for k in ('format','confidence','warnings')})))
            response_id = cur.lastrowid
            for row_no, item in enumerate(marked, 1):
                cur.execute("INSERT INTO bank_response_rows (response_id,table_name,row_no,data,row_key,is_duplicate,duplicate_reason,included) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)", (response_id, item["table"], row_no, json.dumps(item["data"]), item["rowKey"], item["isDuplicate"], item["duplicateReason"], item["included"]))
                item["id"] = cur.lastrowid
            conn.commit()
    graph = get_case_graph(user_id, upload_id)
    req_bank = None
    if requisition_id is not None:
        with get_connection() as conn:
            with conn.cursor(dictionary=True) as cur:
                cur.execute("SELECT bank_name FROM requisitions WHERE id=%s AND upload_id=%s AND supabase_user_id=%s", (requisition_id, upload_id, user_id))
                req = cur.fetchone()
                req_bank = req.get("bank_name") if req else None
    parsed_rows = [item["data"] for item in marked if item["table"] == "lien_transactions"]
    row_ids = {_clean_id(row.get("transaction_id"), FlowConfig()) for row in parsed_rows if row.get("transaction_id")}
    row_accounts = {str(row.get("account_no") or "").replace(" ", "").lower() for row in parsed_rows if row.get("account_no")}
    row_banks = {normalize_bank_name(str(row.get("bank_fi") or "")) for row in parsed_rows}
    candidates = []
    for node in graph.get("nodes", []):
        node_utr = _clean_id(node.get("utr"), FlowConfig())
        node_account = str(node.get("accountNo") or "").replace(" ", "").lower()
        node_bank = normalize_bank_name(str(node.get("bank") or ""))
        score = (6 if node_utr and node_utr in row_ids else 0) + (5 if node_account and node_account in row_accounts else 0)
        score += 4 if node_bank != "__unknown__" and node_bank in row_banks else 0
        score += 3 if req_bank and node_bank == normalize_bank_name(req_bank) else 0
        if node.get("completeness", {}).get("status") == "incomplete": score += 2
        if score:
            candidates.append({"nodeId": node["id"], "bank": node.get("bank"), "layer": node.get("layer"), "incomplete": node.get("completeness", {}).get("status") == "incomplete", "score": score})
    candidates.sort(key=lambda item: (not item["incomplete"], -item["score"], item["layer"], item["nodeId"]))
    min_response_layer = min((int(row.get("layer") or 0) for row in parsed_rows if str(row.get("layer") or "").isdigit()), default=1)
    suggested = candidates[0] if candidates else None
    suggested_shift = suggested["layer"] + 1 - min_response_layer if suggested else 0
    return {"responseId": response_id, **parsed, "rows": marked, "anchorCandidates": candidates, "suggestedAnchorNodeId": suggested["nodeId"] if suggested else None, "suggestedLayerShift": suggested_shift}


def list_responses(user_id: str, upload_id: str) -> dict[str, Any]:
    with get_connection() as conn:
        with conn.cursor(dictionary=True) as cur:
            _require_upload_owner(cur, user_id, upload_id)
            cur.execute("SELECT id,status,requisition_id,anchor_node_id,layer_shift,created_at,applied_at,parse_report FROM bank_responses WHERE upload_id=%s AND supabase_user_id=%s ORDER BY created_at DESC", (upload_id, user_id))
            return {"responses": cur.fetchall()}


def update_row(user_id: str, upload_id: str, response_id: int, row_id: int, changes: dict[str, Any]) -> dict[str, Any]:
    with get_connection() as conn:
        with conn.cursor(dictionary=True) as cur:
            response = _owned_response(cur, user_id, upload_id, response_id)
            if response["status"] != "parsed":
                raise ValueError("Only parsed responses can be edited.")
            cur.execute("SELECT data,table_name,included FROM bank_response_rows WHERE id=%s AND response_id=%s LIMIT 1", (row_id, response_id))
            row = cur.fetchone()
            if row is None: raise ValueError("Response row was not found.")
            data = json.loads(row["data"]) if isinstance(row["data"], str) else row["data"]
            if "included" in changes: included = bool(changes.pop("included"))
            else: included = None
            for key, value in changes.items():
                if key not in data: continue
                data[key] = str(value).replace("\x00", "")[:500] if isinstance(value, str) else value
            _validate_response_row(row["table_name"], data, require_layer=included is not False)
            values = [json.dumps(data), row_key(row["table_name"], data), row_id]
            sql = "UPDATE bank_response_rows SET data=%s,row_key=%s" + (",included=%s" if included is not None else "") + " WHERE id=%s AND response_id=%s"
            if included is not None: values.insert(2, included)
            values.append(response_id); cur.execute(sql, tuple(values)); conn.commit()
            return {"id": row_id, "data": data, "included": included if included is not None else bool(row["included"])}


def add_row(user_id: str, upload_id: str, response_id: int, table_name: str, data: dict[str, Any]) -> dict[str, Any]:
    if table_name not in {"lien_transactions", "hold_accounts"}: raise ValueError("Unsupported response table.")
    with get_connection() as conn:
        with conn.cursor(dictionary=True) as cur:
            response = _owned_response(cur, user_id, upload_id, response_id)
            if response["status"] != "parsed": raise ValueError("Only parsed responses can be edited.")
            clean = {str(key)[:80]: str(value).replace("\x00", "")[:500] if isinstance(value, str) else value for key, value in data.items()}
            _validate_response_row(table_name, clean, require_layer=False)
            cur.execute("SELECT row_key FROM bank_response_rows r JOIN bank_responses b ON b.id=r.response_id WHERE b.upload_id=%s AND b.status='applied'", (upload_id,))
            existing_keys = {item["row_key"] for item in cur.fetchall()}
            cur.execute("SELECT n.utr FROM nodes n JOIN cases c ON c.id=n.case_id WHERE c.upload_id=%s AND c.supabase_user_id=%s AND c.revision=(SELECT MAX(c2.revision) FROM cases c2 WHERE c2.upload_id=c.upload_id AND c2.supabase_user_id=c.supabase_user_id)", (upload_id, user_id))
            existing_utrs = {_clean_id(item["utr"], FlowConfig()) for item in cur.fetchall() if item.get("utr")}
            marked = mark_duplicates([(table_name, clean)], existing_keys, existing_utrs)[0]
            cur.execute("SELECT COALESCE(MAX(row_no),0)+1 AS row_no FROM bank_response_rows WHERE response_id=%s", (response_id,)); row_no = cur.fetchone()["row_no"]
            cur.execute("INSERT INTO bank_response_rows (response_id,table_name,row_no,data,row_key,is_duplicate,duplicate_reason,included) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)", (response_id, table_name, row_no, json.dumps(clean), marked["rowKey"], marked["isDuplicate"], marked["duplicateReason"], marked["included"]))
            row_id = cur.lastrowid; conn.commit()
            return {"id": row_id, "table": table_name, "data": clean, "isDuplicate": marked["isDuplicate"], "duplicateReason": marked["duplicateReason"], "included": marked["included"]}


def _validate_response_row(table_name: str, data: dict[str, Any], *, require_layer: bool = True) -> None:
    allowed = {
        "lien_transactions": {"s_no", "bank_fi", "account_no", "ifsc_code", "layer", "transaction_id", "transaction_datetime", "transaction_amount", "disputed_amount", "reference_remarks", "action_taken_by", "date_of_action"},
        "hold_accounts": {"s_no", "account_no", "hold_date", "hold_amount", "reference_remarks", "action_taken_by", "date_of_action"},
    }[table_name]
    unknown = set(data) - allowed
    if unknown:
        raise ValueError(f"Unsupported {table_name} field: {sorted(unknown)[0]}.")
    if table_name == "lien_transactions" and (require_layer or data.get("layer") not in (None, "")):
        try:
            if int(data.get("layer")) < 1:
                raise ValueError
        except (TypeError, ValueError):
            raise ValueError("Lien transaction layer must be a positive whole number.")
    for field in ("transaction_amount", "disputed_amount", "hold_amount"):
        if data.get(field) not in (None, ""):
            try:
                from decimal import Decimal, InvalidOperation
                amount = Decimal(str(data[field]).replace(",", ""))
                if not amount.is_finite() or amount < 0:
                    raise InvalidOperation
            except (InvalidOperation, ValueError):
                raise ValueError(f"{field.replace('_', ' ').capitalize()} must be a non-negative amount.")
