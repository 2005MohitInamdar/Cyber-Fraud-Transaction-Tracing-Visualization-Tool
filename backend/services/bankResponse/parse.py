"""Parse a bank response using the existing NCRP PDF pipeline stages only."""

from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path
from typing import Any

from services.pdf_extraction.extractor import extract_tables
from services.pdf_extraction.normalization import normalize_directory
from services.pdf_extraction.correct_columns import map_directory
from services.pdf_extraction.transaction_flow import _bank_words, _clean_id, _decimal, _normal_account, FlowConfig
import pdfplumber

MAX_RESPONSE_BYTES = 10 * 1024 * 1024
MAX_RESPONSE_PAGES = 60
_FLOW_CONFIG = FlowConfig()


def _identity(table: str, row: dict[str, Any]) -> tuple[Any, ...]:
    if table == "hold_accounts":
        return (_normal_account(row.get("account_no")), row.get("hold_date"), str(_decimal(row.get("hold_amount"))))
    return (
        row.get("layer"),
        tuple(sorted(set(_bank_words(row.get("bank_fi"), _FLOW_CONFIG)))),
        _normal_account(row.get("account_no")),
        _clean_id(row.get("transaction_id"), _FLOW_CONFIG),
        str(_decimal(row.get("transaction_amount"))),
        str(_decimal(row.get("disputed_amount"))),
    )


def row_key(table: str, row: dict[str, Any]) -> str:
    return hashlib.sha1(repr(_identity(table, row)).encode("utf-8")).hexdigest()


def _read_payloads(directory: Path) -> tuple[dict[str, list[dict[str, Any]]], list[str]]:
    tables: dict[str, list[dict[str, Any]]] = {"lien_transactions": [], "hold_accounts": []}
    warnings: list[str] = []
    for path in directory.glob("*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        table = payload.get("table") if isinstance(payload, dict) else None
        if table not in tables:
            continue
        report = payload.get("_schema_mapping", {})
        if report.get("status") not in (None, "ok"):
            warnings.append(f"{table}: schema mapping {report.get('status')}")
        tables[table].extend(row for row in payload.get("rows", []) if isinstance(row, dict))
    return tables, warnings


def parse_response(pdf_path: str | Path) -> dict[str, Any]:
    """Never raise for user supplied content; return reviewable unknown format instead."""
    path = Path(pdf_path)
    try:
        if path.stat().st_size > MAX_RESPONSE_BYTES:
            return {"format": "unknown", "rows": [], "warnings": ["PDF exceeds 10 MB limit"], "confidence": "low"}
        with pdfplumber.open(path) as pdf:
            if len(pdf.pages) > MAX_RESPONSE_PAGES:
                return {"format": "unknown", "rows": [], "warnings": ["PDF exceeds 60 page limit"], "confidence": "low"}
        with tempfile.TemporaryDirectory(prefix="bank-response-") as temp:
            root = Path(temp)
            raw, normal, mapped = root / "raw", root / "normalized", root / "mapped"
            extract_tables(path, raw)
            normalize_directory(raw, normal)
            map_directory(normal, mapped)
            tables, warnings = _read_payloads(mapped)
            rows = sum(tables.values(), [])
            if not rows:
                return {"format": "unknown", "rows": [], "warnings": ["No transaction tables recognised", *warnings], "confidence": "low"}
            confidence = "low" if warnings else "high"
            return {"format": "ncrp_table", "lienRows": tables["lien_transactions"], "holdRows": tables["hold_accounts"], "warnings": warnings, "confidence": confidence}
    except Exception as exc:  # parsing failures are always an officer-review state
        print(f"[BANK RESPONSE PARSE] {exc}")
        return {"format": "unknown", "rows": [], "warnings": ["No transaction tables recognised"], "confidence": "low"}


def mark_duplicates(rows: list[tuple[str, dict[str, Any]]], existing_keys: set[str], existing_utrs: set[str]) -> list[dict[str, Any]]:
    """Return review records, keeping the first occurrence in a response."""
    seen: set[str] = set()
    marked: list[dict[str, Any]] = []
    for table, row in rows:
        key = row_key(table, row)
        utr = _clean_id(row.get("transaction_id"), _FLOW_CONFIG)
        duplicate_reason = None
        if key in existing_keys or key in seen:
            duplicate_reason = "same row"
        elif table == "lien_transactions" and utr and utr in existing_utrs:
            duplicate_reason = "UTR already in case"
        seen.add(key)
        marked.append({"table": table, "data": row, "rowKey": key, "isDuplicate": bool(duplicate_reason), "duplicateReason": duplicate_reason, "included": not bool(duplicate_reason)})
    return marked
