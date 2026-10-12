"""Backfill mapped extraction JSON into case_source_payloads for existing cases."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))
from db.connection import get_connection
from services.fileupload.paths import parse_upload_id, safe_join

EXTRACTED_DIR = BACKEND_DIR / "uploads" / "extracted"
EXCLUDED = {"transaction_flow.json", "no_transaction_records.json"}


def _load_payloads(upload_id: str) -> list[tuple[str, str]]:
    directory = safe_join(EXTRACTED_DIR, parse_upload_id(upload_id))
    payloads = []
    if not directory.is_dir():
        return payloads
    for path in sorted(directory.glob("*.json")):
        if path.name in EXCLUDED:
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        table = payload.get("table") if isinstance(payload, dict) else None
        if isinstance(table, str) and table:
            payloads.append((table, json.dumps(payload, ensure_ascii=False)))
    return payloads


def _backfill(upload_id: str) -> int:
    payloads = _load_payloads(upload_id)
    if not payloads:
        print(f"{upload_id}: no mapped JSON payloads found")
        return 0
    with get_connection() as conn:
        with conn.cursor(dictionary=True) as cur:
            cur.execute(
                "SELECT supabase_user_id FROM fraud_case_uploads WHERE upload_id=%s LIMIT 1",
                (upload_id,),
            )
            owner = cur.fetchone()
            if owner is None:
                print(f"{upload_id}: upload owner not found; skipped")
                return 0
            cur.executemany(
                "INSERT INTO case_source_payloads (upload_id,supabase_user_id,table_name,payload) "
                "VALUES (%s,%s,%s,%s) ON DUPLICATE KEY UPDATE "
                "supabase_user_id=VALUES(supabase_user_id),payload=VALUES(payload)",
                [(upload_id, owner["supabase_user_id"], table, payload) for table, payload in payloads],
            )
        conn.commit()
    print(f"{upload_id}: backfilled {len(payloads)} mapped payload(s)")
    return len(payloads)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("upload_id", nargs="?", help="upload UUID to backfill")
    parser.add_argument("--all", action="store_true", help="backfill every extracted upload directory")
    args = parser.parse_args()
    if bool(args.upload_id) == bool(args.all):
        parser.error("provide one upload_id or --all")
    if args.all:
        upload_ids = []
        if EXTRACTED_DIR.exists():
            for child in sorted(EXTRACTED_DIR.iterdir()):
                if child.is_dir():
                    try:
                        upload_ids.append(parse_upload_id(child.name))
                    except ValueError:
                        continue
    else:
        upload_ids = [parse_upload_id(args.upload_id)]
    for upload_id in upload_ids:
        _backfill(upload_id)


if __name__ == "__main__":
    main()
