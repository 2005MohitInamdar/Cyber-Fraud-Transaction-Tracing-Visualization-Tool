"""Immutable case-revision rebuilds from original and reviewed bank-response rows."""
from __future__ import annotations

import json
import tempfile
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from db.connection import get_connection, get_redis
from services.caseDetail.detail import invalidate_case_detail
from services.caseGraph.graph import get_case_graph
from services.pdf_extraction.commit_to_db import (
    EDGE_COLUMNS, HOLD_COLUMNS, HOLD_LINK_COLUMNS, NODE_COLUMNS, REJECTED_COLUMNS,
    _dec, _edge_rows, _hold_link_rows, _hold_rows, _json, _node_rows, _rejected_rows,
    _flag, _insert_many,
)
from services.pdf_extraction.correct_columns import TABLE_SCHEMAS, META_SCHEMA
from services.pdf_extraction.transaction_flow import build_transaction_flow
from services.upload_progress import publish
from services.fileupload.paths import parse_upload_id, safe_join

_TABLES = ("lien_transactions", "hold_accounts", "complaint_transactions", "failed_transactions", "pending_transactions", "amount_summary", "no_action_references", "complaint_meta")
_LIEN_COLUMNS = ("s_no", "bank_fi", "account_no", "ifsc_code", "layer", "transaction_id", "transaction_datetime", "transaction_amount", "disputed_amount", "reference_remarks", "action_taken_by", "date_of_action")
_BACKEND_DIR = Path(__file__).resolve().parents[2]
_EXTRACTED_DIR = _BACKEND_DIR / "uploads" / "extracted"


def _mapped_columns(table: str) -> list[str]:
    if table == "lien_transactions":
        return list(_LIEN_COLUMNS)
    if table == "complaint_meta":
        return [target for target, _source, _cast in META_SCHEMA]
    return [target for target, _source, _cast in TABLE_SCHEMAS.get(table, [])]


def _value(value: Any) -> Any:
    if isinstance(value, Decimal): return f"{value:.2f}"
    if isinstance(value, datetime): return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, date): return value.isoformat()
    return value


def _decode_payload(value: Any) -> dict[str, Any] | None:
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8")
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return None
    return value if isinstance(value, dict) else None


def _has_original_transactions(payloads: dict[str, dict[str, Any]]) -> bool:
    return any(payloads.get(table, {}).get("rows") for table in ("lien_transactions", "complaint_transactions"))


def _read_extracted_payloads(upload_id: str) -> dict[str, dict[str, Any]]:
    canonical_id = parse_upload_id(upload_id)
    payloads: dict[str, dict[str, Any]] = {}
    for table in _TABLES:
        path = safe_join(_EXTRACTED_DIR, canonical_id, f"{table}.json")
        if not path.is_file():
            continue
        payload = _decode_payload(path.read_text(encoding="utf-8"))
        if payload and payload.get("table") == table:
            payloads[table] = payload
    return payloads


def export_original_rows(user_id: str, upload_id: str) -> dict[str, dict[str, Any]]:
    """Export original mapped rows, preferring durable JSON over optional legacy tables."""
    with get_connection() as conn:
        with conn.cursor(dictionary=True) as cur:
            cur.execute("SELECT table_name,payload FROM case_source_payloads WHERE upload_id=%s AND supabase_user_id=%s", (upload_id, user_id))
            payloads = {
                row["table_name"]: payload
                for row in cur.fetchall()
                if row.get("table_name") in _TABLES
                and (payload := _decode_payload(row.get("payload"))) is not None
            }
            if _has_original_transactions(payloads):
                print("[bank-response] Original rows source: case_source_payloads")
                return payloads

            legacy_payloads: dict[str, dict[str, Any]] = {}
            for table in _TABLES:
                cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema=DATABASE() AND table_name=%s", (table,))
                if cur.fetchone() is None: continue
                sql = f"SELECT * FROM `{table}` WHERE upload_id=%s AND supabase_user_id=%s"
                sql += " ORDER BY complaint_meta_id DESC LIMIT 1" if table == "complaint_meta" else " ORDER BY s_no"
                cur.execute(sql, (upload_id, user_id))
                rows = cur.fetchall()
                if not rows:
                    continue
                mapped_columns = _mapped_columns(table)
                clean = [{key: _value(row.get(key)) for key in mapped_columns} for row in rows]
                if table == "complaint_meta":
                    legacy_payloads[table] = {"table": table, "data": clean[0]}
                else:
                    columns = mapped_columns
                    legacy_payloads[table] = {"table": table, "columns": columns, "rows": clean}
            if _has_original_transactions(legacy_payloads):
                print("[bank-response] Original rows source: legacy extraction tables")
                return legacy_payloads

    file_payloads = _read_extracted_payloads(upload_id)
    if _has_original_transactions(file_payloads):
        print("[bank-response] Original rows source: uploads/extracted/<upload_id>/*.json")
        return file_payloads
    raise ValueError(
        "Cannot apply bank response: original lien_transactions or complaint_transactions "
        "were not found in case_source_payloads, legacy tables, or extracted JSON files."
    )


def response_rows_for_build(cur, response: dict[str, Any], originals: dict[str, dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    cur.execute("SELECT table_name,data FROM bank_response_rows WHERE response_id=%s AND included=1 AND is_duplicate=0 ORDER BY row_no", (response["id"],))
    output = {"lien_transactions": [], "hold_accounts": []}
    rows = cur.fetchall()
    lien_rows = [(table, json.loads(data) if isinstance(data, str) else data) for table, data in ((row["table_name"], row["data"]) for row in rows)]
    def shifted_layer(row: dict[str, Any]) -> int:
        try:
            layer = int(row.get("layer"))
        except (TypeError, ValueError):
            raise ValueError("Every included lien row needs a numeric layer.")
        shifted = layer + int(response.get("layer_shift") or 0)
        if shifted < 1:
            raise ValueError("Response layers must stay at least 1.")
        return shifted

    minimum = min((shifted_layer(row) for table, row in lien_rows if table == "lien_transactions"), default=None)
    for table, row in lien_rows:
        row = dict(row)
        if table == "lien_transactions":
            row["layer"] = shifted_layer(row)
            if response.get("anchor_node_id") and row["layer"] == minimum:
                row["explicit_parent"] = {"nodeId": response["anchor_node_id"]}
        output[table].append(row)
    for table, additions in output.items():
        base = originals.setdefault(table, {"table": table, "columns": list(additions[0]) if additions else [], "rows": []})
        rows = base.setdefault("rows", [])
        next_s_no = max((int(row.get("s_no") or 0) for row in rows), default=0)
        for row in additions:
            next_s_no += 1
            row["s_no"] = next_s_no
            rows.append(row)
    return output


def rebuild_case(user_id: str, upload_id: str, response_id: int, change_note: str | None = None, annotation: dict[str, Any] | None = None, anchor_node_id: str | None = None, layer_shift: int | None = None) -> dict[str, Any]:
    redis = get_redis(); lock = f"rebuild:{upload_id}"
    if not redis.set(lock, "1", nx=True, ex=120): raise RuntimeError("REBUILD_BUSY")
    try:
        publish(upload_id, "Rebuilding transaction flow")
        with get_connection() as conn:
            try:
              with conn.cursor(dictionary=True) as cur:
                cur.execute("SELECT * FROM bank_responses WHERE id=%s AND upload_id=%s AND supabase_user_id=%s FOR UPDATE", (response_id, upload_id, user_id)); response = cur.fetchone()
                if response is None: raise PermissionError("You do not have access to this bank response.")
                if response["status"] != "parsed": raise RuntimeError("ALREADY_APPLIED")
                cur.execute("SELECT * FROM cases WHERE upload_id=%s AND supabase_user_id=%s ORDER BY revision DESC LIMIT 1", (upload_id, user_id)); previous = cur.fetchone()
                if previous is None: raise ValueError("No original case exists for this upload.")
                cur.execute("SELECT COUNT(*) AS count FROM nodes WHERE case_id=%s", (previous["id"],)); previous_node_count = int(cur.fetchone()["count"])
                cur.execute("SELECT COUNT(*) AS count FROM bank_response_rows WHERE response_id=%s AND included=1 AND is_duplicate=0", (response_id,)); row_count = int(cur.fetchone()["count"])
                if row_count == 0 and not annotation: raise ValueError("Nothing new to apply")
                cur.execute("SELECT MIN(CAST(JSON_UNQUOTE(JSON_EXTRACT(data, '$.layer')) AS SIGNED)) AS min_layer FROM bank_response_rows WHERE response_id=%s AND table_name='lien_transactions' AND included=1 AND is_duplicate=0", (response_id,)); minimum = cur.fetchone()["min_layer"]
                shift = int(layer_shift or 0)
                if anchor_node_id:
                    cur.execute("SELECT layer FROM nodes WHERE case_id=%s AND node_id=%s", (previous["id"], anchor_node_id)); anchor = cur.fetchone()
                    if anchor is None: raise ValueError("Anchor node does not belong to the current case revision.")
                    shift = int(layer_shift) if layer_shift is not None else int(anchor["layer"]) + 1 - int(minimum or 1)
                    if minimum is not None and int(minimum) + shift != int(anchor["layer"]) + 1:
                        raise ValueError("Anchored response must begin in the layer immediately after its anchor.")
                elif layer_shift is not None:
                    shift = int(layer_shift)
                if annotation:
                    if annotation.get("kind") not in {"no_onward", "withdrawn", "lien_confirmed"}:
                        raise ValueError("Invalid annotation kind.")
                    cur.execute("SELECT 1 FROM nodes WHERE case_id=%s AND node_id=%s", (previous["id"], str(annotation.get("nodeId") or "")))
                    if cur.fetchone() is None: raise ValueError("Annotation node does not belong to the current case revision.")
                if minimum is not None and int(minimum) + shift < 1:
                    raise ValueError("Response layers must stay at least 1.")
                cur.execute("UPDATE bank_responses SET anchor_node_id=%s,layer_shift=%s WHERE id=%s", (anchor_node_id, shift, response_id))
                response["anchor_node_id"], response["layer_shift"] = anchor_node_id, shift
                publish(upload_id, "Loading original case rows")
                originals = export_original_rows(user_id, upload_id)
                cur.execute("SELECT * FROM bank_responses WHERE upload_id=%s AND supabase_user_id=%s AND status='applied' ORDER BY applied_at,id", (upload_id, user_id))
                applied = cur.fetchall()
                for item in [*applied, response]: response_rows_for_build(cur, item, originals)
                with tempfile.TemporaryDirectory(prefix="case-rebuild-") as temp:
                    directory = Path(temp)
                    for table, payload in originals.items(): (directory / f"{table}.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
                    publish(upload_id, "Reconstructing transaction flow")
                    build_transaction_flow(directory)
                    flow = json.loads((directory / "transaction_flow.json").read_text(encoding="utf-8"))
                rebuilt_node_count = len(flow.get("nodes", {}))
                if rebuilt_node_count < previous_node_count:
                    raise RuntimeError(
                        "REBUILD_NODE_COUNT_REGRESSION: rebuilt graph has fewer nodes than the previous revision."
                    )
                revision = int(previous.get("revision") or 1) + 1
                checks = flow.get("checks", {})
                publish(upload_id, f"Saving case revision {revision}")
                cur.execute("INSERT INTO cases (supabase_user_id,upload_id,ack_no,status,base_debit_total,reported_fraud_total,hold_total,reported_lien_total,holds_match_lien,checks,raw_flow,report_extras,revision,parent_case_id,change_note) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", (user_id, upload_id, previous.get("ack_no"), previous.get("status"), _dec(checks.get("baseDebitTotal")), _dec(checks.get("reportedFraudTotal")), _dec(checks.get("holdTotal")), _dec(checks.get("reportedLienTotal")), _flag(checks.get("holdsMatchReportedLien")), _json(checks), _json(flow), previous.get("report_extras"), revision, previous["id"], change_note))
                case_id = cur.lastrowid
                for table, columns, rows in (("nodes", NODE_COLUMNS, _node_rows(case_id, flow)), ("edges", EDGE_COLUMNS, _edge_rows(case_id, flow)), ("holds", HOLD_COLUMNS, _hold_rows(case_id, flow)), ("hold_links", HOLD_LINK_COLUMNS, _hold_link_rows(case_id, flow)), ("rejected_candidates", REJECTED_COLUMNS, _rejected_rows(case_id, flow))): _insert_many(cur, table, columns, rows)
                if annotation:
                    cur.execute(
                        "INSERT INTO node_annotations (upload_id,supabase_user_id,node_id,kind,note,response_id,case_revision) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                        (upload_id, user_id, annotation["nodeId"], annotation["kind"], str(annotation.get("note") or "")[:500] or None, response_id, revision),
                    )
                cur.execute("UPDATE bank_responses SET status='applied',applied_at=CURRENT_TIMESTAMP WHERE id=%s", (response_id,))
                cur.execute("SELECT node_id FROM nodes WHERE case_id=%s", (previous["id"],)); old_ids = {row["node_id"] for row in cur.fetchall()}
                cur.execute("SELECT node_id FROM nodes WHERE case_id=%s", (case_id,)); new_ids = sorted(row["node_id"] for row in cur.fetchall() if row["node_id"] not in old_ids)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        parse_report = response.get("parse_report") or {}
        if isinstance(parse_report, (bytes, bytearray)):
            parse_report = parse_report.decode("utf-8", errors="replace")
        if isinstance(parse_report, str):
            try: parse_report = json.loads(parse_report)
            except json.JSONDecodeError: parse_report = {}
        warnings = parse_report.get("warnings", []) if isinstance(parse_report, dict) else []
        resolved_ids: list[str] = []
        try:
            previous_graph = get_case_graph(user_id, upload_id, revision=int(previous.get("revision") or 1))
            rebuilt_graph = get_case_graph(user_id, upload_id, revision=revision)
            was_incomplete = {item["nodeId"] for item in previous_graph.get("incomplete", []) if item.get("nodeId")}
            still_incomplete = {item["nodeId"] for item in rebuilt_graph.get("incomplete", []) if item.get("nodeId")}
            resolved_ids = sorted(was_incomplete - still_incomplete)
        except Exception as exc:
            warnings = [*warnings, f"Revision saved, but resolved-item counts could not be refreshed: {exc}"]
        try:
            invalidate_case_detail(upload_id)
            redis.delete(f"case:{upload_id}:graph:v2", f"case:{upload_id}:graph:v3", f"case:{upload_id}:graph:v3:r{revision}")
        except Exception as exc:
            warnings = [*warnings, f"Revision saved, but cached views could not be invalidated: {exc}"]
        try:
            publish(upload_id, f"Saved revision {revision}", "complete")
        except Exception:
            pass
        return {"revision": revision, "caseId": case_id, "newNodeIds": new_ids, "resolvedNodeIds": resolved_ids, "warnings": warnings, "counts": {"newNodeCount": len(new_ids), "resolvedNodeCount": len(resolved_ids)}}
    except Exception:
        try:
            publish(upload_id, "Rebuild failed. The previous case revision is unchanged.", "failed")
        except Exception:
            pass
        raise
    finally:
        redis.delete(lock)
