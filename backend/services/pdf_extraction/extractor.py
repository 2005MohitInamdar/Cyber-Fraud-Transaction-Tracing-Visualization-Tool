"""
PDF Table Extractor
====================
Extracts fixed-schema tables from the complaint-report PDF using pdfplumber
and writes one JSON file per table. Fully standalone — no other modules
required.

Tables extracted (always present, fixed columns):
  1. complaint_transactions  - main complainant transaction list
  2. pending_transactions    - transactions pending at banks
  3. amount_summary          - summary of amounts put on hold
  4. lien_transactions       - detailed lien/action table
  5. hold_accounts           - per-account hold details
  6. failed_transactions     - failed / zero-amount transactions
  7. no_action_references    - reference nos with no action taken
  8. complaint_meta          - complaint acceptance meta-data

Why this doesn't key off page numbers
----------------------------------------
Real complaint reports vary in page count and table layout depending on
how much data each section has (more lien rows pushes everything after it
onto fewer/more pages, tables split mid-row across a page boundary, etc).
So instead of assuming "table X always lives on page Y", every table
pdfplumber finds on every page is classified by its *header content* (see
`_classify_table`), not by its position.

A table whose first row doesn't match any known header is treated as a
continuation of whichever table was most recently classified — this is
how pdfplumber represents a table that got cut off mid-page-break: the
continuation table has no header row of its own, just more data rows
with the same column count as the table it continues.

Usage (standalone):
    python extractor.py <path_to_pdf> [output_directory]

    If output_directory is omitted, JSON files are written to a folder called
    `extracted_tables/` placed next to the PDF file.
"""

import json
import re
import sys
from pathlib import Path

import pdfplumber


# ── Cell / header cleanup ──────────────────────────────────────────────────────

def _clean(value) -> str:
    """Collapse newlines / extra whitespace inside a cell value."""
    if value is None:
        return ""
    text = str(value)
    text = re.sub(r"[\r\n\t]+", " ", text)
    text = re.sub(r" {2,}", " ", text)
    return text.strip()


def _clean_header(header: list) -> list:
    """Return a list of cleaned header strings."""
    return [_clean(h) for h in header]


def _rows_to_dicts(header: list, rows: list) -> list:
    """
    Convert raw table rows to a list of dicts keyed by header names.
    Duplicate column names get a numeric suffix (_2, _3, ...).
    """
    seen = {}
    unique_header = []
    for col in header:
        if col in seen:
            seen[col] += 1
            unique_header.append(f"{col}_{seen[col]}")
        else:
            seen[col] = 1
            unique_header.append(col)

    records = []
    for row in rows:
        record = {}
        for col, val in zip(unique_header, row):
            record[col] = _clean(val)
        records.append(record)
    return records


def _save_json(data: dict, output_dir: Path, filename: str) -> Path:
    """Write *data* as pretty JSON to output_dir/filename; return the path."""
    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / filename
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return out_path


# ── Table classification ───────────────────────────────────────────────────────
#
# Every table pdfplumber returns is classified by looking at its *header
# row's content* — not its page number or table index. A row is only
# treated as a header if it matches one of these signatures; anything else
# is assumed to be a continuation of the previously-classified table (see
# the main loop in extract_tables()).
#
# Order matters: rules are checked top-to-bottom, first match wins. More
# specific / distinctive signatures are checked before looser ones (e.g.
# hold_accounts and failed_transactions both have 7 columns and share
# several header words, so hold_accounts' "put on hold" marker — which is
# unique to it — is checked before the looser failed_transactions rule).

_META_KEYS = {
    "complaint accepted by",
    "complaint accepted date",
    "current status",
    "under process",
}

_ID_COLUMNS = {
    "complaint_transactions": "S No.",
    "pending_transactions":   "S No.",
    "amount_summary":         "S No.",
    "lien_transactions":      "S No.",
    "hold_accounts":          "S No.",
    "failed_transactions":    "S No.",
    "no_action_references":   "S No.",
}

_TABLE_FILENAMES = {
    "complaint_transactions": "complaint_transactions.json",
    "pending_transactions":   "pending_transactions.json",
    "amount_summary":         "amount_summary.json",
    "lien_transactions":      "lien_transactions.json",
    "hold_accounts":          "hold_accounts.json",
    "failed_transactions":    "failed_transactions.json",
    "no_action_references":   "no_action_references.json",
    "complaint_meta":         "complaint_meta.json",
}


def _classify_table(cleaned_header: list) -> str | None:
    """
    Return the table_name this header row belongs to, or None if it
    doesn't look like a header at all (i.e. it's actual data — a
    continuation row from a table whose header appeared on an earlier
    page/table).
    """
    cols = len(cleaned_header)
    norm = " | ".join(c.lower() for c in cleaned_header)
    norm = re.sub(r"\s+", " ", norm)

    # complaint_meta: a flat 2-column key/value table. Every "row" is a
    # key/value pair, including what pdfplumber treats as the header row.
    if cols == 2:
        first_cell = re.sub(r"\s+", " ", cleaned_header[0].lower())
        if any(k in first_cell for k in _META_KEYS):
            return "complaint_meta"

    if "ifsc" in norm:
        return "lien_transactions"

    if "put on hold" in norm:
        return "hold_accounts"

    if (
        "amount" in norm
        and "account no" in norm
        and "put on hold" not in norm
        and "ifsc" not in norm
        and "wallet" not in norm
        and "card details" not in norm
        and "pending" not in norm
        and 6 <= cols <= 8
    ):
        return "failed_transactions"

    if (
        cols <= 4
        and "reference no" in norm
        and "action taken" in norm
        and "account no" not in norm
    ):
        return "no_action_references"

    if "pending" in norm:
        return "pending_transactions"

    if "card details" in norm or ("wallet" in norm and "transacti" in norm):
        return "complaint_transactions"

    if "amount" in norm and cols <= 3:
        return "amount_summary"

    return None


def extract_tables(pdf_path, output_dir=None) -> dict:
    """
    Open *pdf_path*, extract all known tables (regardless of which pages
    they fall on), write one JSON file per table into *output_dir*, and
    return a mapping of {table_name: json_path}.

    Parameters
    ----------
    pdf_path   : str | Path  – path to the complaint PDF file
    output_dir : str | Path  – directory for JSON output.
                               Defaults to <pdf_dir>/extracted_tables/

    Returns
    -------
    dict  { table_name (str) : output_path (Path) }
    """
    pdf_path = Path(pdf_path)
    if not pdf_path.exists():
        raise FileNotFoundError(f"PDF not found: {pdf_path}")

    if output_dir is None:
        output_dir = pdf_path.parent / "extracted_tables"
    output_dir = Path(output_dir)

    written = {}

    # Accumulated state, built up across every table on every page.
    table_headers: dict[str, list] = {}    # table_name -> header cell list (first time seen)
    table_raw_rows: dict[str, list] = {}   # table_name -> list of raw data rows (no header)
    meta_rows: list = []                   # complaint_meta: list of raw [key, value, ...] rows
    active_type: str | None = None         # most recently classified table — continuation target

    with pdfplumber.open(pdf_path) as pdf:
        pages = pdf.pages
        print(f"  [extractor] PDF opened — {len(pages)} page(s) found: {pdf_path}")

        if not pages:
            print("  [extractor] WARNING: PDF has no pages — skipping extraction")
            return written

        for page_idx, page in enumerate(pages):
            page_tables = page.extract_tables()
            if not page_tables:
                continue

            for raw in page_tables:
                if not raw or not raw[0]:
                    continue

                header_row = raw[0]
                cleaned_header = _clean_header(header_row)
                table_type = _classify_table(cleaned_header)

                # ── complaint_meta: flat key/value pairs, every row counts ──
                if table_type == "complaint_meta":
                    for row in raw:
                        if len(row) >= 2:
                            meta_rows.append(row)
                    active_type = None
                    continue

                # ── recognised header: start or continue this table type ───
                if table_type is not None:
                    if table_type not in table_headers:
                        table_headers[table_type] = cleaned_header
                        table_raw_rows[table_type] = []
                    # `raw[0]` was a genuine header row here, so only the
                    # rows after it are data.
                    table_raw_rows[table_type].extend(raw[1:])
                    active_type = table_type
                    continue

                # ── unrecognised header: likely a continuation table ───────
                # pdfplumber has no way to know a table is "the same table,
                # continued" — it just returns a fresh table per page with
                # no header row. We detect this by column count matching
                # the currently active table.
                if active_type is not None:
                    expected_cols = len(table_headers[active_type])
                    if len(header_row) == expected_cols:
                        # every row here is data — nothing to skip
                        table_raw_rows[active_type].extend(raw)
                        continue

                print(
                    f"  [extractor] WARNING: unrecognised table on page "
                    f"{page_idx + 1} ({len(header_row)} cols) — skipped: "
                    f"{cleaned_header}"
                )

        # ── Convert rows and save each accumulated table ───────────────────────
        for table_name, header in table_headers.items():
            rows = _rows_to_dicts(header, table_raw_rows[table_name])
            payload = {
                "table":   table_name,
                "columns": header,
                "rows":    rows,
            }
            written[table_name] = _save_json(
                payload, output_dir, _TABLE_FILENAMES[table_name]
            )

        # ── complaint_meta: flat key/value dict ────────────────────────────────
        if meta_rows:
            meta = {}
            for row in meta_rows:
                key = _clean(row[0])
                val = _clean(row[1]) if len(row) >= 2 else ""
                if key:
                    meta[key] = val
            payload = {"table": "complaint_meta", "data": meta}
            written["complaint_meta"] = _save_json(
                payload, output_dir, "complaint_meta.json"
            )

    return written


def main():
    if len(sys.argv) < 2:
        print("Usage: python extractor.py <pdf_path> [output_directory]")
        sys.exit(1)

    pdf_path = sys.argv[1]
    output_dir = sys.argv[2] if len(sys.argv) >= 3 else None

    print(f"Extracting tables from: {pdf_path}")
    written = extract_tables(pdf_path, output_dir)

    print(f"\nExtracted {len(written)} table(s):")
    for name, path in written.items():
        print(f"  [{name}]  ->  {path}")


if __name__ == "__main__":
    main()