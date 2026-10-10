#!/usr/bin/env python
"""
scripts/discover_bank_contacts.py
───────────────────────────────────
CLI to discover and import bank contact emails.

Usage
-----
# Discover from a text file (one bank name per line):
    python scripts/discover_bank_contacts.py --from-file banks.txt

# Discover from every distinct bank already in the DB:
    python scripts/discover_bank_contacts.py --from-db

# Import a reviewed CSV (with verified=1 on approved rows):
    python scripts/discover_bank_contacts.py --import reviewed.csv

The discovery output is written to bank_contacts_review.csv for human review.
After reviewing, mark rows you approve with verified=1 and re-import.

CSV columns
-----------
bank_name, normalized_name, email, confidence, generic, source_url, evidence, verified
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from pathlib import Path

# Allow running from repo root
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from dotenv import load_dotenv
load_dotenv()

from backend.services.email.bank_names import normalize_bank_name
from services.requisition.discovery import discover_bank_contact, Candidate
from services.requisition.contacts import _upsert_contact

_OUTPUT_FILE = "bank_contacts_review.csv"
_FIELDNAMES  = ["bank_name", "normalized_name", "email", "confidence", "generic", "source_url", "evidence", "verified"]


def _get_banks_from_db() -> list[str]:
    from db.connection import get_connection
    banks: set[str] = set()
    with get_connection() as conn:
        with conn.cursor(dictionary=True) as cur:
            cur.execute("SELECT DISTINCT bank FROM nodes WHERE bank IS NOT NULL AND bank != ''")
            for row in cur.fetchall():
                banks.add(row["bank"])
            cur.execute("SELECT DISTINCT action_taken_by FROM holds WHERE action_taken_by IS NOT NULL AND action_taken_by != ''")
            for row in cur.fetchall():
                banks.add(row["action_taken_by"])
    return sorted(banks)


def _discover(bank_names: list[str]) -> None:
    print(f"[DISCOVER] Running discovery for {len(bank_names)} banks …")
    rows: list[dict] = []

    for i, bank_name in enumerate(bank_names, start=1):
        norm = normalize_bank_name(bank_name)
        print(f"  [{i}/{len(bank_names)}] {bank_name!r} ({norm})")
        candidates: list[Candidate] = []
        try:
            candidates = discover_bank_contact(bank_name)
        except Exception as exc:
            print(f"    ERROR: {exc}")

        if not candidates:
            print(f"    → No candidates found")
            rows.append({
                "bank_name": bank_name, "normalized_name": norm,
                "email": "", "confidence": "", "generic": "",
                "source_url": "", "evidence": "No candidates found", "verified": 0,
            })
        else:
            for c in candidates:
                print(f"    → {c.email}  conf={c.confidence:.2f}  generic={c.generic}")
                rows.append({
                    "bank_name":      bank_name,
                    "normalized_name": norm,
                    "email":          c.email,
                    "confidence":     f"{c.confidence:.2f}",
                    "generic":        int(c.generic),
                    "source_url":     c.source_url,
                    "evidence":       c.evidence,
                    "verified":       0,
                })

    with open(_OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=_FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)

    print(f"\n[DISCOVER] Written to {_OUTPUT_FILE}")
    print("Review the CSV, set verified=1 on rows you approve, then run:")
    print(f"  python scripts/discover_bank_contacts.py --import {_OUTPUT_FILE}")


def _import_csv(path: str) -> None:
    from db.connection import get_connection
    print(f"[IMPORT] Reading {path} …")
    imported = 0
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if not row.get("email") or not row.get("bank_name"):
                continue
            verified_flag = str(row.get("verified", "0")).strip() == "1"
            norm = row.get("normalized_name") or normalize_bank_name(row["bank_name"])
            cand = Candidate(
                email      = row["email"].strip().lower(),
                source_url = row.get("source_url", ""),
                confidence = float(row["confidence"]) if row.get("confidence") else 0.0,
                evidence   = row.get("evidence", ""),
                generic    = str(row.get("generic", "0")).strip() == "1",
            )
            source = "directory" if verified_flag else "discovered"
            _upsert_contact(norm, row["bank_name"], cand, source=source)
            if verified_flag:
                # Also mark verified in DB
                from services.requisition.contacts import get_contacts_for_bank, mark_verified
                contacts = get_contacts_for_bank(norm)
                for c in contacts:
                    if c["email"] == cand.email:
                        mark_verified(c["id"], "cli-import")
                        break
            imported += 1

    print(f"[IMPORT] Imported {imported} rows.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Discover and import bank contact emails.")
    group  = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--from-file", metavar="FILE", help="Text file with one bank name per line.")
    group.add_argument("--from-db",   action="store_true", help="Use all distinct bank names from the DB.")
    group.add_argument("--import",    metavar="CSV",  dest="import_csv", help="Import a reviewed CSV.")
    args = parser.parse_args()

    if args.import_csv:
        _import_csv(args.import_csv)
    elif args.from_file:
        banks = [l.strip() for l in open(args.from_file).readlines() if l.strip()]
        _discover(banks)
    elif args.from_db:
        banks = _get_banks_from_db()
        print(f"[DISCOVER] Found {len(banks)} distinct banks in DB.")
        _discover(banks)


if __name__ == "__main__":
    main()
