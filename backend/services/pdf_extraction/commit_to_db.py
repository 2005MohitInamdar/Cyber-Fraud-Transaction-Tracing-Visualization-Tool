# """
# Load db_schema JSON into MySQL - INSERT only
# ==============================================

# Reads the JSON files produced by schema_mapping.py (stage 3 of the
# pipeline) and inserts their rows into the matching MySQL tables using
# plain parameterized INSERT statements. No CREATE TABLE, no TRUNCATE, no
# ON DUPLICATE KEY UPDATE - just INSERT, since the tables already exist
# from the DDL you ran separately.

# Requires: mysql-connector-python
#     pip install mysql-connector-python

# Fill in DB_CONFIG below with your host/user/password/database, then:

#     python load_to_mysql.py <db_schema_dir>

# Each JSON file's "columns" list determines both the column order in the
# INSERT statement and the order values are pulled from each row, so this
# follows schema_mapping.py's output exactly - if you rename a column
# there, no change is needed here.

# complaint_meta is handled separately: it's a single key/value record
# (AUTO_INCREMENT id, no s_no), so it gets one INSERT with all four fields.
# """

# import argparse
# import json
# import sys
# from pathlib import Path
# from fastapi import Request,HTTPException, status
# import mysql.connector
# from mysql.connector import Error as MySQLError
# import os
# from dotenv import load_dotenv
# # from ..auth import get_current_user
# load_dotenv()
# # ── Fill these in ────────────────────────────────────────────────────────────
# DB_CONFIG = {
#     "host":os.getenv("DB_HOST"),
#     "port":int(os.getenv("DB_PORT", 3306)),
#     "user":os.getenv("DB_USER"),
#     "password":os.getenv("DB_PASSWORD"),
#     "database":os.getenv("DB_NAME"),
# }


# def get_connection():
#     """Single place the connection is opened - edit DB_CONFIG above."""
#     conn = mysql.connector.connect(
#         host=DB_CONFIG["host"],
#         port=DB_CONFIG["port"],
#         user=DB_CONFIG["user"],
#         password=DB_CONFIG["password"],
#         database=DB_CONFIG["database"],
#     )
#     return conn


# # Insert order doesn't matter here (no foreign keys in this schema), but
# # a fixed order makes the run's console output predictable.
# TABLE_ORDER = [
#     "amount_summary",
#     "complaint_transactions",
#     "failed_transactions",
#     "hold_accounts",
#     "lien_transactions",
#     "no_action_references",
#     "pending_transactions",
#     "complaint_meta",
# ]


# def load_json_files(db_schema_dir: Path) -> dict:
#     """Return {table_name: payload_dict} for every recognized JSON file."""
#     payloads = {}
#     for path in sorted(Path(db_schema_dir).glob("*.json")):
#         with open(path, encoding="utf-8") as f:
#             payload = json.load(f)
#         table = payload.get("table")
#         if table:
#             payloads[table] = payload
#     return payloads


# def build_row_insert(table: str, columns: list) -> str:
#     """
#     Plain INSERT INTO <table> (<columns>) VALUES (<placeholders>).
#     No ON DUPLICATE KEY UPDATE, no IGNORE - a duplicate primary key will
#     raise and stop that table's insert, which is the correct behavior for
#     a first-time load.
#     """
#     col_list = ", ".join(f"`{c}`" for c in columns)
#     placeholders = ", ".join(["%s"] * len(columns))
#     return f"INSERT INTO `{table}` ({col_list}) VALUES ({placeholders})"


# def insert_row_table(
#     cursor, table: str, payload: dict, user_id: str, upload_id: str
# ) -> int:
#     """Insert extracted rows and associate every row with its upload."""
#     # Do not mutate the JSON payload's column list: it can be reused by a
#     # caller, and the database-specific ownership columns belong only here.
#     columns = [*payload.get("columns", []), "supabase_user_id", "upload_id"]

#     rows = payload.get("rows", [])
#     if not columns or not rows:
#         return 0

#     sql = build_row_insert(table, columns)
#     values = [
#         tuple(row.get(col) for col in columns[:-2]) + (user_id, upload_id)
#         for row in rows
#     ]

#     cursor.executemany(sql, values)
#     return cursor.rowcount


# def insert_meta_table(
#     cursor, table: str, payload: dict, user_id: str, upload_id: str
# ) -> int:
#     """complaint_meta: single key/value record, one INSERT."""
#     data = payload.get("data", {})
#     # columns.append("supabase_user_id")

#     if not data:
#         return 0

#     columns = [*data.keys(), "supabase_user_id", "upload_id"]
#     values = [data[c] for c in columns[:-2]] + [user_id, upload_id]

#     sql = build_row_insert(table, columns)
#     cursor.execute(sql, values)
#     return cursor.rowcount



# def load_all(user_id: str, upload_id: str, db_schema_dir: str, progress=None) -> dict:
#     payloads = load_json_files(db_schema_dir)
    
#     conn = get_connection()
#     cursor = conn.cursor()
    
#     results = {}
#     try:
#         for table in TABLE_ORDER:
#             payload = payloads.get(table)
#             if payload is None:
#                 print(f"  [load] SKIP {table} - no JSON file found")
#                 continue

#             try:
#                 if table == "complaint_meta":
#                     inserted = insert_meta_table(cursor, table, payload, user_id, upload_id)
#                 else:
#                     inserted = insert_row_table(cursor, table, payload, user_id, upload_id)
#                 conn.commit()
#                 results[table] = inserted
#                 if progress:
#                     progress(f"Saved {inserted} record(s) to {table.replace('_', ' ')}.")
#                 print(f"  [load] {table:<24} {inserted} row(s) inserted")
#             except MySQLError as e:
#                 conn.rollback()
#                 results[table] = f"FAILED: {e}"
#                 if progress:
#                     progress(f"Could not save {table.replace('_', ' ')}.", "failed")
#                 print(f"  [load] {table:<24} FAILED - {e}")
#     finally:
#         cursor.close()
#         conn.close()

#     return results


"""
Load the NCRP transaction flow into MySQL
=========================================

Pipeline:  schema_mapping.py  ->  <db_schema_dir>/*.json
           extraction_flow.py ->  <db_schema_dir>/transaction_flow.json
           this file          ->  MySQL (cases, nodes, edges, holds, hold_links,
                                          rejected_candidates)

load_all() builds transaction_flow.json from the mapped JSON files (rebuild=True by
default, so it can never be stale), then inserts one case inside ONE transaction: either the whole
case is saved or nothing is. The flow goes into the new tables; the mapped rows are ALSO written to
the original per-section tables (amount_summary, lien_transactions, ...) so the existing case-detail
page keeps working (legacy_tables=True; columns the old tables don't have are skipped). Plain parameterized INSERTs only - no CREATE TABLE,
no TRUNCATE, no ON DUPLICATE KEY UPDATE. The tables must already exist
(run the DDL, then the cases-table migration below).

Migration for the ownership columns (run once, on an empty `cases` table):

    ALTER TABLE cases
      ADD COLUMN supabase_user_id VARCHAR(64) NOT NULL AFTER id,
      ADD COLUMN upload_id        VARCHAR(64) NOT NULL AFTER supabase_user_id,
      ADD COLUMN report_extras    JSON NULL,
      MODIFY ack_no VARCHAR(32) NULL,
      DROP INDEX uq_cases_ack,
      ADD UNIQUE KEY uq_cases_upload (upload_id),
      ADD KEY idx_cases_user_ack (supabase_user_id, ack_no);

Requires: mysql-connector-python, python-dotenv
Usage:    python load_to_mysql.py <db_schema_dir> --user-id U --upload-id X [--ack-no N] [--replace]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable

import mysql.connector
from dotenv import load_dotenv
from mysql.connector import Error as MySQLError

import importlib


def _import_flow_builder():
    """Find build_transaction_flow whether this runs inside the app package or as a script.
    The module is called transaction_flow in the app (extraction_flow in older copies)."""
    tried = []
    for name in ("transaction_flow", "extraction_flow"):
        for target, package in ((f".{name}", __package__), (name, None)):
            if target.startswith(".") and not package:
                continue                                   # relative import needs a package
            try:
                return importlib.import_module(target, package).build_transaction_flow
            except (ImportError, AttributeError):
                tried.append(target)
    raise ImportError(f"build_transaction_flow not found (tried: {', '.join(tried)})")


build_transaction_flow = _import_flow_builder()

load_dotenv()

# -- connection ---------------------------------------------------------------
DB_CONFIG = {
    "host": os.getenv("DB_HOST"),
    "port": int(os.getenv("DB_PORT", 3306)),
    "user": os.getenv("DB_USER"),
    "password": os.getenv("DB_PASSWORD"),
    "database": os.getenv("DB_NAME"),
}


def get_connection():
    """Single place the connection is opened - edit DB_CONFIG / .env."""
    missing = [k for k in ("host", "user", "password", "database") if not DB_CONFIG.get(k)]
    if missing:
        raise RuntimeError(f"Missing DB settings: {', '.join(missing)} (set DB_HOST/DB_USER/DB_PASSWORD/DB_NAME)")
    return mysql.connector.connect(**DB_CONFIG)


# Small report sections that are not part of the flow graph. They are kept as
# JSON on the case row so nothing from the report is lost.
EXTRA_TABLES = (
    "amount_summary",
    "failed_transactions",
    "pending_transactions",
    "no_action_references",
    "complaint_meta",
)

# The original per-section tables, keyed by supabase_user_id + upload_id. The case-detail page
# still reads them, so they keep receiving the mapped rows (see _insert_legacy).
LEGACY_TABLES = (
    "amount_summary",
    "complaint_transactions",
    "failed_transactions",
    "hold_accounts",
    "lien_transactions",
    "no_action_references",
    "pending_transactions",
    "complaint_meta",
)

# -- value helpers ------------------------------------------------------------


def _dec(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def _flag(value: Any) -> int | None:
    return None if value is None else int(bool(value))


def _json(value: Any) -> str | None:
    return None if value is None else json.dumps(value, ensure_ascii=False)


def build_row_insert(table: str, columns: list[str]) -> str:
    """Plain INSERT INTO <table> (<columns>) VALUES (<placeholders>)."""
    col_list = ", ".join(f"`{c}`" for c in columns)
    placeholders = ", ".join(["%s"] * len(columns))
    return f"INSERT INTO `{table}` ({col_list}) VALUES ({placeholders})"


def _insert_many(cursor, table: str, columns: list[str], rows: list[tuple]) -> int:
    if not rows:
        return 0
    cursor.executemany(build_row_insert(table, columns), rows)
    return len(rows)


# -- reading the mapped files -------------------------------------------------


def _read_extras(directory: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return (extras, meta). extras = small report sections; meta = complaint_meta key/values."""
    extras: dict[str, Any] = {}
    meta: dict[str, Any] = {}
    for path in sorted(directory.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        table = payload.get("table") if isinstance(payload, dict) else None
        if table not in EXTRA_TABLES:
            continue
        if "rows" in payload:
            extras[table] = payload["rows"]
        elif "data" in payload:
            extras[table] = payload["data"]
            if table == "complaint_meta" and isinstance(payload["data"], dict):
                meta = payload["data"]
    return extras, meta


def _pick_ack(meta: dict[str, Any], explicit: str | None) -> str | None:
    if explicit:
        return re.sub(r"\s+", "", str(explicit))
    for key, value in meta.items():
        if "ack" in str(key).lower() and value:
            return re.sub(r"\s+", "", str(value))
    return None


# -- flattening transaction_flow.json into table rows --------------------------

CASE_COLUMNS = ["supabase_user_id", "upload_id", "ack_no", "status", "base_debit_total",
                "reported_fraud_total", "hold_total", "reported_lien_total", "holds_match_lien",
                "checks", "raw_flow", "report_extras"]
NODE_COLUMNS = ["case_id", "node_id", "layer", "bank", "action_taken_by", "account_no", "utr",
                "tx_amount", "disputed_amount", "amount_estimated", "frozen_amount",
                "unaccounted_amount", "embedded_ids", "root_ids", "source_row_ids", "remarks"]
EDGE_COLUMNS = ["case_id", "from_node", "to_node", "match_rule", "confidence", "amount_passed",
                "ambiguous", "merged", "amount_estimated"]
HOLD_COLUMNS = ["case_id", "hold_id", "account_no", "hold_amount", "hold_date", "action_taken_by",
                "embedded_ids", "source_row_ids", "remarks"]
HOLD_LINK_COLUMNS = ["case_id", "hold_id", "node_id", "match_rule", "confidence", "amount"]
REJECTED_COLUMNS = ["case_id", "from_node", "to_node", "reason"]


def _node_rows(case_id: int, flow: dict[str, Any]) -> list[tuple]:
    return [(case_id, n["id"], n["layer"], n.get("bank"), n.get("actionTakenBy"), n.get("account"),
             n.get("transactionId"), _dec(n.get("transactionAmount")), _dec(n.get("disputedAmount")),
             _flag(n.get("amountEstimated")) or 0, _dec(n.get("frozenAmount")) or Decimal("0"),
             _dec(n.get("unaccountedAmount")), _json(n.get("embeddedIds")), _json(n.get("rootIds")),
             _json(n.get("sourceRowIds")), n.get("remarks") or None)
            for n in flow.get("nodes", {}).values()]


def _edge_rows(case_id: int, flow: dict[str, Any]) -> list[tuple]:
    # a connection's "to" is a list; one database row per parent->child pair
    return [(case_id, c["from"], target, c["matchRule"], _dec(c["confidence"]), _dec(c.get("amountPassed")),
             _flag(c.get("ambiguous")) or 0, _flag(c.get("merged")) or 0, _flag(c.get("amountEstimated")) or 0)
            for c in flow.get("connections", []) for target in c["to"]]


def _hold_rows(case_id: int, flow: dict[str, Any]) -> list[tuple]:
    return [(case_id, h["id"], h.get("account"), _dec(h.get("holdAmount")), h.get("holdDate") or None,
             h.get("actionTakenBy"), _json(h.get("embeddedIds")), _json(h.get("sourceRowIds")),
             h.get("remarks") or None)
            for h in flow.get("holds", {}).values()]


def _hold_link_rows(case_id: int, flow: dict[str, Any]) -> list[tuple]:
    return [(case_id, l["hold"], l["node"], l["matchRule"], _dec(l["confidence"]), _dec(l.get("amount")))
            for l in flow.get("holdLinks", [])]


def _rejected_rows(case_id: int, flow: dict[str, Any]) -> list[tuple]:
    return [(case_id, r["from"], r["to"], r["reason"]) for r in flow.get("rejectedCandidates", [])]


# -- legacy per-section tables -------------------------------------------------


def _read_legacy_payloads(directory: Path) -> dict[str, dict[str, Any]]:
    payloads: dict[str, dict[str, Any]] = {}
    for path in sorted(directory.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        table = payload.get("table") if isinstance(payload, dict) else None
        if table in LEGACY_TABLES:
            payloads[table] = payload
    return payloads


def _table_columns(cursor, table: str) -> set[str]:
    cursor.execute("SELECT COLUMN_NAME FROM information_schema.COLUMNS "
                   "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s", (table,))
    return {row[0] for row in cursor.fetchall()}


def _insert_legacy(cursor, table: str, payload: dict[str, Any], user_id: str, upload_id: str) -> int:
    """Insert one mapped section into its original table. Only columns that table really has are
    used, so adding a field to the mapped JSON (e.g. acknowledgement_no in complaint_meta) can never
    break the old table; skipped columns are printed."""
    existing = _table_columns(cursor, table)
    if not existing:
        print(f"  [load] {table:<24} SKIP - table does not exist")
        return 0
    if table == "complaint_meta":
        data = payload.get("data") or {}
        names = [c for c in data if c in existing]
        rows = [tuple(data[c] for c in names) + (user_id, upload_id)] if names else []
        wanted = list(data)
    else:
        wanted = payload.get("columns", [])
        names = [c for c in wanted if c in existing]
        rows = [tuple(row.get(c) for c in names) + (user_id, upload_id) for row in payload.get("rows", [])]
    skipped = [c for c in wanted if c not in existing]
    if skipped:
        print(f"  [load] {table:<24} skipped columns not in table: {', '.join(skipped)}")
    return _insert_many(cursor, table, names + ["supabase_user_id", "upload_id"], rows) if rows else 0


def _delete_legacy(cursor, user_id: str, upload_id: str) -> None:
    for table in LEGACY_TABLES:
        if _table_columns(cursor, table):
            cursor.execute(f"DELETE FROM `{table}` WHERE `upload_id` = %s AND `supabase_user_id` = %s",
                           (upload_id, user_id))


# -- main entry point ---------------------------------------------------------


def load_all(user_id: str, upload_id: str, db_schema_dir: str | Path,
             progress: Callable[..., None] | None = None, *,
             ack_no: str | None = None, replace: bool = False, rebuild: bool = True,
             legacy_tables: bool = True) -> dict[str, Any]:
    """
    Build the flow from the mapped JSON files and save it as one case.

    Returns {"case_id": int, "cases": 1, "nodes": n, "edges": n, "holds": n,
             "hold_links": n, "rejected_candidates": n, <legacy table>: n, ...}
    or, on failure, {"<step>": "FAILED: <reason>"} with nothing saved.
    replace=True first deletes an earlier case for the same upload_id (its rows go with it),
    including that upload's rows in the legacy per-section tables.
    legacy_tables=False skips the legacy per-section tables once nothing reads them any more.
    rebuild=False reuses an existing transaction_flow.json (use it when the pipeline already
    built the flow); the flow is still built if the file is missing.
    """
    directory = Path(db_schema_dir)
    say = progress or (lambda *args, **kwargs: None)

    try:                                                   # 1. build + read the flow
        flow_file = directory / "transaction_flow.json"
        if rebuild or not flow_file.exists():
            build_transaction_flow(directory, progress=progress)
        flow = json.loads(flow_file.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - reported to the caller, nothing saved yet
        say("Could not build the transaction flow.", "failed")
        print(f"  [load] transaction_flow       FAILED - {exc}")
        return {"transaction_flow": f"FAILED: {exc}"}

    extras, meta = _read_extras(directory)
    checks = flow.get("checks", {})
    case_row = (user_id, upload_id, _pick_ack(meta, ack_no), meta.get("current_status"),
                _dec(checks.get("baseDebitTotal")), _dec(checks.get("reportedFraudTotal")),
                _dec(checks.get("holdTotal")), _dec(checks.get("reportedLienTotal")),
                _flag(checks.get("holdsMatchReportedLien")), _json(checks), _json(flow), _json(extras))

    conn = get_connection()
    cursor = conn.cursor()
    results: dict[str, Any] = {}
    step = "cases"
    try:
        if replace:                                        # ON DELETE CASCADE clears the child rows
            cursor.execute("DELETE FROM `cases` WHERE `upload_id` = %s", (upload_id,))
            if legacy_tables:
                _delete_legacy(cursor, user_id, upload_id)
        cursor.execute(build_row_insert("cases", CASE_COLUMNS), case_row)
        case_id = cursor.lastrowid
        results["cases"] = 1

        for step, columns, rows in (                       # parents before children (foreign keys)
            ("nodes", NODE_COLUMNS, _node_rows(case_id, flow)),
            ("edges", EDGE_COLUMNS, _edge_rows(case_id, flow)),
            ("holds", HOLD_COLUMNS, _hold_rows(case_id, flow)),
            ("hold_links", HOLD_LINK_COLUMNS, _hold_link_rows(case_id, flow)),
            ("rejected_candidates", REJECTED_COLUMNS, _rejected_rows(case_id, flow)),
        ):
            results[step] = _insert_many(cursor, step, columns, rows)

        if legacy_tables:                                  # same transaction: all or nothing
            legacy = _read_legacy_payloads(directory)
            for step in LEGACY_TABLES:
                if step in legacy:
                    results[step] = _insert_legacy(cursor, step, legacy[step], user_id, upload_id)
        conn.commit()
    except MySQLError as exc:
        conn.rollback()
        say(f"Could not save {step.replace('_', ' ')}.", "failed")
        print(f"  [load] {step:<24} FAILED - {exc}")
        return {step: f"FAILED: {exc}"}
    finally:
        cursor.close()
        conn.close()

    results = {"case_id": case_id, **results}
    for table, count in results.items():
        if table != "case_id":
            say(f"Saved {count} record(s) to {table.replace('_', ' ')}.")
            print(f"  [load] {table:<24} {count} row(s) inserted")
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Load an NCRP transaction flow into MySQL.")
    parser.add_argument("db_schema_dir")
    parser.add_argument("--user-id", required=True)
    parser.add_argument("--upload-id", required=True)
    parser.add_argument("--ack-no")
    parser.add_argument("--replace", action="store_true", help="replace an earlier case for this upload id")
    args = parser.parse_args()
    results = load_all(args.user_id, args.upload_id, args.db_schema_dir, ack_no=args.ack_no, replace=args.replace)
    sys.exit(1 if any(isinstance(v, str) and v.startswith("FAILED") for v in results.values()) else 0)


if __name__ == "__main__":
    main()