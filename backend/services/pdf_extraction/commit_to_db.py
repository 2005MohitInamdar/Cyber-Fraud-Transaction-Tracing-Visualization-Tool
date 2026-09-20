"""
Load db_schema JSON into MySQL - INSERT only
==============================================

Reads the JSON files produced by schema_mapping.py (stage 3 of the
pipeline) and inserts their rows into the matching MySQL tables using
plain parameterized INSERT statements. No CREATE TABLE, no TRUNCATE, no
ON DUPLICATE KEY UPDATE - just INSERT, since the tables already exist
from the DDL you ran separately.

Requires: mysql-connector-python
    pip install mysql-connector-python

Fill in DB_CONFIG below with your host/user/password/database, then:

    python load_to_mysql.py <db_schema_dir>

Each JSON file's "columns" list determines both the column order in the
INSERT statement and the order values are pulled from each row, so this
follows schema_mapping.py's output exactly - if you rename a column
there, no change is needed here.

complaint_meta is handled separately: it's a single key/value record
(AUTO_INCREMENT id, no s_no), so it gets one INSERT with all four fields.
"""

import argparse
import json
import sys
from pathlib import Path
from fastapi import Request,HTTPException, status
import mysql.connector
from mysql.connector import Error as MySQLError
import os
from dotenv import load_dotenv
# from ..auth import get_current_user
load_dotenv()
# ── Fill these in ────────────────────────────────────────────────────────────
DB_CONFIG = {
    "host":os.getenv("DB_HOST"),
    "port":int(os.getenv("DB_PORT", 3306)),
    "user":os.getenv("DB_USER"),
    "password":os.getenv("DB_PASSWORD"),
    "database":os.getenv("DB_NAME"),
}


def get_connection():
    """Single place the connection is opened - edit DB_CONFIG above."""
    conn = mysql.connector.connect(
        host=DB_CONFIG["host"],
        port=DB_CONFIG["port"],
        user=DB_CONFIG["user"],
        password=DB_CONFIG["password"],
        database=DB_CONFIG["database"],
    )
    return conn


# Insert order doesn't matter here (no foreign keys in this schema), but
# a fixed order makes the run's console output predictable.
TABLE_ORDER = [
    "amount_summary",
    "complaint_transactions",
    "failed_transactions",
    "hold_accounts",
    "lien_transactions",
    "no_action_references",
    "pending_transactions",
    "complaint_meta",
]


def load_json_files(db_schema_dir: Path) -> dict:
    """Return {table_name: payload_dict} for every recognized JSON file."""
    payloads = {}
    for path in sorted(Path(db_schema_dir).glob("*.json")):
        with open(path, encoding="utf-8") as f:
            payload = json.load(f)
        table = payload.get("table")
        if table:
            payloads[table] = payload
    return payloads


def build_row_insert(table: str, columns: list) -> str:
    """
    Plain INSERT INTO <table> (<columns>) VALUES (<placeholders>).
    No ON DUPLICATE KEY UPDATE, no IGNORE - a duplicate primary key will
    raise and stop that table's insert, which is the correct behavior for
    a first-time load.
    """
    col_list = ", ".join(f"`{c}`" for c in columns)
    placeholders = ", ".join(["%s"] * len(columns))
    return f"INSERT INTO `{table}` ({col_list}) VALUES ({placeholders})"


def insert_row_table(
    cursor, table: str, payload: dict, user_id: str, upload_id: str
) -> int:
    """Insert extracted rows and associate every row with its upload."""
    # Do not mutate the JSON payload's column list: it can be reused by a
    # caller, and the database-specific ownership columns belong only here.
    columns = [*payload.get("columns", []), "supabase_user_id", "upload_id"]

    rows = payload.get("rows", [])
    if not columns or not rows:
        return 0

    sql = build_row_insert(table, columns)
    values = [
        tuple(row.get(col) for col in columns[:-2]) + (user_id, upload_id)
        for row in rows
    ]

    cursor.executemany(sql, values)
    return cursor.rowcount


def insert_meta_table(
    cursor, table: str, payload: dict, user_id: str, upload_id: str
) -> int:
    """complaint_meta: single key/value record, one INSERT."""
    data = payload.get("data", {})
    # columns.append("supabase_user_id")

    if not data:
        return 0

    columns = [*data.keys(), "supabase_user_id", "upload_id"]
    values = [data[c] for c in columns[:-2]] + [user_id, upload_id]

    sql = build_row_insert(table, columns)
    cursor.execute(sql, values)
    return cursor.rowcount



def load_all(user_id: str, upload_id: str, db_schema_dir: str, progress=None) -> dict:
    payloads = load_json_files(db_schema_dir)
    
    conn = get_connection()
    cursor = conn.cursor()
    
    results = {}
    try:
        for table in TABLE_ORDER:
            payload = payloads.get(table)
            if payload is None:
                print(f"  [load] SKIP {table} - no JSON file found")
                continue

            try:
                if table == "complaint_meta":
                    inserted = insert_meta_table(cursor, table, payload, user_id, upload_id)
                else:
                    inserted = insert_row_table(cursor, table, payload, user_id, upload_id)
                conn.commit()
                results[table] = inserted
                if progress:
                    progress(f"Saved {inserted} record(s) to {table.replace('_', ' ')}.")
                print(f"  [load] {table:<24} {inserted} row(s) inserted")
            except MySQLError as e:
                conn.rollback()
                results[table] = f"FAILED: {e}"
                if progress:
                    progress(f"Could not save {table.replace('_', ' ')}.", "failed")
                print(f"  [load] {table:<24} FAILED - {e}")
    finally:
        cursor.close()
        conn.close()

    return results


# def main():
#     parser = argparse.ArgumentParser(
#         description="Insert db_schema JSON output into MySQL (INSERT only)."
#     )
#     parser.add_argument("db_schema_dir", help="Folder of schema-mapped JSON files")
#     args = parser.parse_args()

#     print(f"Loading {args.db_schema_dir} -> "
#           f"{DB_CONFIG['user']}@{DB_CONFIG['host']}:{DB_CONFIG['port']}/"
#           f"{DB_CONFIG['database']}")
#     results = load_all(args.db_schema_dir)

#     failed = [t for t, r in results.items() if isinstance(r, str) and r.startswith("FAILED")]
#     sys.exit(1 if failed else 0)


# if __name__ == "__main__":
#     main()
