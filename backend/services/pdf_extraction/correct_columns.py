# # """
# # correct_columns.py
# # ==================
# # Remaps raw PDF-extracted column names (as produced by extractor.py) to the
# # MySQL column names defined in gam_db schema, then writes the cleaned JSON
# # files back to the same output directory (in-place overwrite).

# # Special handling
# # ----------------
# # lien_transactions
# #     The PDF merges "Account No.", "IFSC Code", and "Layer" into a single
# #     cell:  "876005973 IPOS00001 Layer :1"
# #     This module splits that cell into three separate MySQL columns:
# #         account_no  → everything before the IFSC-like token
# #         ifsc_code   → the IFSC-like token (4 alpha + digits, ~11 chars)
# #         layer       → the integer after "Layer :"

# # Public API
# # ----------
# #     from services.pdf_extraction.correct_columns import remap_tables

# #     remap_tables(output_dir)          # remaps + overwrites JSON in place
# #     remap_tables(output_dir, dry_run=True)  # returns result without writing

# # Standalone usage
# # ----------------
# #     cd backend/
# #     python services/pdf_extraction/correct_columns.py services/pdf_extraction/output
# # """

# # from __future__ import annotations

# # import json
# # import re
# # import sys
# # from pathlib import Path
# # from typing import Any
# # from services.pdf_extraction.commit_to_db import commit_to_db

# # # ── Column-name mapping ────────────────────────────────────────────────────────
# # #
# # # Each entry maps:   raw_pdf_column_name  →  mysql_column_name
# # #
# # # Keys are stripped/normalised before lookup (see _norm_key).
# # # Tables not listed here have no column renames needed.

# # _COLUMN_MAP: dict[str, dict[str, str]] = {

# #     "amount_summary": {
# #         "S No.":   "s_no",
# #         "":        "description",   # empty-string header = description column
# #         "Amount":  "amount",
# #     },

# #     "complaint_meta": {
# #         # complaint_meta uses a flat key→value dict, not a rows list.
# #         # Keys are remapped by _remap_complaint_meta().
# #         "Complaint Accepted By":   "complaint_accepted_by",
# #         "Complaint Accepted Date": "complaint_accepted_date",
# #         "Current Status":          "current_status",
# #         "Under Process":           "under_process_date",
# #     },

# #     "complaint_transactions": {
# #         "S No":                        "s_no",
# #         "Account No./ Wallet ID":      "account_wallet_id",
# #         "Transacti on ID":             "transaction_id",
# #         "Card Details":                "card_details",
# #         "Transacti on Amount":         "transaction_amount",
# #         "Reference No.":               "reference_no",
# #         "Transacti on Date & Time":    "transaction_datetime",
# #         "Complaint Date":              "complaint_date",
# #         "Bank/FIs":                    "bank_fi",
# #     },

# #     "failed_transactions": {
# #         "S No.":                    "s_no",
# #         "Account No.":              "account_no",
# #         "Date":                     "transaction_date",
# #         "Transaction Amount":       "transaction_amount",
# #         "Reference No / Remarks":   "reference_remarks",
# #         "Action Taken By":          "action_taken_by",
# #         "Date of Action":           "date_of_action",
# #     },

# #     "hold_accounts": {
# #         "S No.":                    "s_no",
# #         "Account No.":              "account_no",
# #         "Put on hold Date":         "hold_date",
# #         "Put on hold Amount":       "hold_amount",
# #         "Reference No / Remarks":   "reference_remarks",
# #         "Action Taken By":          "action_taken_by",
# #         "Date of Action":           "date_of_action",
# #     },

# #     # lien_transactions has extra splitting logic — see _remap_lien_row()
# #     "lien_transactions": {
# #         "S No.":                        "s_no",
# #         "Bank /FIs":                    "bank_fi",
# #         # "Account No./IFSC Code" is SPLIT → account_no + ifsc_code + layer
# #         "Transac tion ID / UTR Number": "transaction_id",
# #         "Transac tion Date & Time":     "transaction_datetime",
# #         "Transacti on Amount":          "transaction_amount",
# #         "Dispute d Amount":             "disputed_amount",
# #         "Referen ce No / Remark s":     "reference_remarks",
# #         "Action Taken By":              "action_taken_by",
# #         "Date of Action":               "date_of_action",
# #     },

# #     "no_action_references": {
# #         "S No.":                    "s_no",
# #         "Reference No / Remarks":   "reference_remarks",
# #         "Action Taken By":          "action_taken_by",
# #         "Date of Action":           "date_of_action",
# #     },

# #     "pending_transactions": {
# #         "S No.":                        "s_no",
# #         "Bank":                         "bank",
# #         "No. of Transaction Pending":   "no_of_transactions_pending",
# #         "Amount Pending":               "amount_pending",
# #         "Pending From":                 "pending_from",
# #     },
# # }


# # # ── Normalisation helper ───────────────────────────────────────────────────────

# # def _norm_key(raw: str) -> str:
# #     """
# #     Collapse runs of whitespace (including newlines from pdfplumber) to a
# #     single space and strip leading/trailing whitespace.

# #     e.g. "Transacti on ID"  →  "Transacti on ID"   (already single-spaced)
# #          "Reference No /\nRemarks"  →  "Reference No / Remarks"
# #     """
# #     return re.sub(r"\s+", " ", raw).strip()


# # # ── lien_transactions account/IFSC/layer splitter ─────────────────────────────

# # # IFSC codes are exactly 11 characters: 4 alpha + 0 + 6 alphanumeric.
# # # In practice the PDF sometimes omits the zero, so we match 4+ alpha chars
# # # followed by word chars to identify the IFSC token.
# # _IFSC_RE = re.compile(r"\b([A-Z]{4}[A-Z0-9]{2,8})\b")
# # _LAYER_RE = re.compile(r"[Ll]ayer\s*:\s*(\d+)")


# # def _split_account_ifsc_layer(raw_cell: str) -> tuple[str, str, str]:
# #     """
# #     Parse the combined "Account No./IFSC Code" cell from lien_transactions.

# #     Examples
# #     --------
# #     "876005973 IPOS00001 Layer :1"
# #         → account_no="876005973", ifsc_code="IPOS00001", layer="1"

# #     "9142500051 YESB0PTMUPI Layer :5"
# #         → account_no="9142500051", ifsc_code="YESB0PTMUPI", layer="5"

# #     "5830017208 Layer :4"           (no IFSC in this row)
# #         → account_no="5830017208", ifsc_code="", layer="4"

# #     Returns (account_no, ifsc_code, layer_str)
# #     """
# #     cell = raw_cell.strip()

# #     # Extract layer number
# #     layer_match = _LAYER_RE.search(cell)
# #     layer_str = layer_match.group(1) if layer_match else ""
# #     # Remove "Layer :N" from cell so it doesn't pollute account/ifsc
# #     cell_no_layer = _LAYER_RE.sub("", cell).strip()

# #     # Extract IFSC code (first all-caps alphanumeric token that looks like IFSC)
# #     ifsc_match = _IFSC_RE.search(cell_no_layer)
# #     if ifsc_match:
# #         ifsc_code = ifsc_match.group(1)
# #         # Remove IFSC token from the remaining string to isolate account_no
# #         account_no = cell_no_layer[:ifsc_match.start()].strip()
# #     else:
# #         ifsc_code = ""
# #         account_no = cell_no_layer.strip()

# #     return account_no, ifsc_code, layer_str


# # # ── Per-row remapping helpers ──────────────────────────────────────────────────

# # def _remap_row(row: dict[str, Any], col_map: dict[str, str]) -> dict[str, Any]:
# #     """
# #     Return a new dict with keys renamed according to col_map.
# #     Keys not found in col_map are dropped (they don't map to a MySQL column).
# #     """
# #     new_row: dict[str, Any] = {}
# #     for raw_key, value in row.items():
# #         normalised = _norm_key(raw_key)
# #         mysql_col = col_map.get(normalised)
# #         if mysql_col is not None:
# #             new_row[mysql_col] = value
# #     return new_row


# # def _remap_lien_row(row: dict[str, Any]) -> dict[str, Any]:
# #     """
# #     Like _remap_row but also splits the combined account/IFSC/layer cell.
# #     """
# #     col_map = _COLUMN_MAP["lien_transactions"]
# #     new_row: dict[str, Any] = {}

# #     for raw_key, value in row.items():
# #         normalised = _norm_key(raw_key)

# #         # Special case: split the combined cell
# #         if normalised == "Account No./IFSC Code":
# #             account_no, ifsc_code, layer_str = _split_account_ifsc_layer(str(value))
# #             new_row["account_no"] = account_no
# #             new_row["ifsc_code"] = ifsc_code
# #             new_row["layer"] = layer_str
# #             continue

# #         mysql_col = col_map.get(normalised)
# #         if mysql_col is not None:
# #             new_row[mysql_col] = value

# #     return new_row


# # def _remap_complaint_meta(data: dict[str, Any]) -> dict[str, Any]:
# #     """
# #     complaint_meta has a flat key→value dict instead of a rows list.
# #     Remap its keys using the same _COLUMN_MAP entry.
# #     """
# #     col_map = _COLUMN_MAP["complaint_meta"]
# #     return {
# #         col_map[_norm_key(k)]: v
# #         for k, v in data.items()
# #         if _norm_key(k) in col_map
# #     }


# # # ── Per-table dispatcher ───────────────────────────────────────────────────────

# # def _remap_table(table_name: str, payload: dict[str, Any]) -> dict[str, Any]:
# #     """
# #     Given the raw JSON payload for *table_name*, return a new payload with
# #     all column names remapped to MySQL names.

# #     The JSON structure is preserved (same top-level keys); only the
# #     'columns' list and the keys inside each 'rows' dict are changed.
# #     complaint_meta uses a 'data' dict instead of 'rows'.
# #     """
# #     if table_name == "complaint_meta":
# #         raw_data = payload.get("data", {})
# #         return {
# #             "table": table_name,
# #             "data": _remap_complaint_meta(raw_data),
# #         }

# #     col_map = _COLUMN_MAP.get(table_name, {})
# #     raw_rows: list[dict] = payload.get("rows", [])

# #     if table_name == "lien_transactions":
# #         remapped_rows = [_remap_lien_row(row) for row in raw_rows]
# #         # Build new column list from the first remapped row
# #         new_columns = list(remapped_rows[0].keys()) if remapped_rows else []
# #     else:
# #         remapped_rows = [_remap_row(row, col_map) for row in raw_rows]
# #         # Remap the top-level 'columns' list as well
# #         new_columns = [
# #             col_map.get(_norm_key(c), _norm_key(c))
# #             for c in payload.get("columns", [])
# #         ]

# #     return {
# #         "table":           table_name,
# #         "columns":         new_columns,
# #         "rows":            remapped_rows,
# #         "extraction_meta": payload.get("extraction_meta"),
# #     }


# # # ── Public API ─────────────────────────────────────────────────────────────────

# # _TABLE_FILES: dict[str, str] = {
# #     "amount_summary":         "amount_summary.json",
# #     "complaint_meta":         "complaint_meta.json",
# #     "complaint_transactions": "complaint_transactions.json",
# #     "failed_transactions":    "failed_transactions.json",
# #     "hold_accounts":          "hold_accounts.json",
# #     "lien_transactions":      "lien_transactions.json",
# #     "no_action_references":   "no_action_references.json",
# #     "pending_transactions":   "pending_transactions.json",
# # }


# # def remap_tables(
# #     output_dir: str | Path,
# #     dry_run: bool = False,
# # ) -> dict[str, dict]:
# #     """
# #     Read every JSON file in *output_dir*, remap column names to MySQL names,
# #     and write the cleaned JSON back to the same file (in-place overwrite).

# #     Parameters
# #     ----------
# #     output_dir : str | Path
# #         Directory containing the raw JSON files produced by extractor.py.
# #     dry_run : bool
# #         If True, return the remapped payloads without writing to disk.

# #     Returns
# #     -------
# #     dict[str, dict]
# #         { table_name: remapped_payload }  for every table that was processed.

# #     Raises
# #     ------
# #     FileNotFoundError
# #         If *output_dir* does not exist.
# #     """
# #     output_dir = Path(output_dir)
# #     if not output_dir.exists():
# #         raise FileNotFoundError(f"output_dir not found: {output_dir}")

# #     result: dict[str, dict] = {}

# #     for table_name, filename in _TABLE_FILES.items():
# #         path = output_dir / filename
# #         if not path.exists():
# #             print(f"  [!]  {table_name}: file not found — skipped ({path})")
# #             continue

# #         with open(path, encoding="utf-8") as f:
# #             raw_payload = json.load(f)

# #         remapped = _remap_table(table_name, raw_payload)
# #         result[table_name] = remapped

# #         if not dry_run:
# #             with open(path, "w", encoding="utf-8") as f:
# #                 json.dump(remapped, f, ensure_ascii=False, indent=2)
# #             print(f"  [OK] {table_name}: columns remapped -> {path.name}")
# #         else:
# #             print(f"  [DRY] {table_name}: would remap {len(remapped.get('rows', remapped.get('data', {})))} row(s)")

# #     return result


# # # ── Standalone runner ──────────────────────────────────────────────────────────

# # if __name__ == "__main__":
# #     output_dir = sys.argv[1] if len(sys.argv) > 1 else "output"
# #     print(f"\nRemapping columns in: {output_dir}\n" + "-" * 50)
# #     remap_tables(output_dir)
# #     print("\nDone.")




# """
# correct_columns.py
# ==================
# Remaps raw PDF-extracted column names (as produced by extractor.py) to the
# MySQL column names defined in gam_db schema, then writes the cleaned JSON
# files back to the same output directory (in-place overwrite).

# Special handling
# ----------------
# lien_transactions
#     The PDF merges "Account No.", "IFSC Code", and "Layer" into a single
#     cell:  "876005973 IPOS00001 Layer :1"
#     This module splits that cell into three separate MySQL columns:
#         account_no  → everything before the IFSC-like token
#         ifsc_code   → the IFSC-like token (4 alpha + digits, ~11 chars)
#         layer       → the integer after "Layer :"

# Public API
# ----------
#     from services.pdf_extraction.correct_columns import remap_tables

#     remap_tables(output_dir)          # remaps + overwrites JSON in place
#     remap_tables(output_dir, dry_run=True)  # returns result without writing

# Standalone usage
# ----------------
#     cd backend/
#     python services/pdf_extraction/correct_columns.py services/pdf_extraction/output

# Note
# ----
# This module is intentionally DB-agnostic — it only reads/writes JSON files
# on disk and never talks to MySQL. Committing extracted data to the database
# is handled by commit_to_db.py, which is called separately (from
# services/fileupload/upload.py, after extract_tables()/remap_tables() have
# already run). Importing commit_to_db here would be pointless coupling:
# remap_tables() supports a dry_run mode that must never touch the database,
# and this file has no need to know the database exists at all.
# """

# from __future__ import annotations

# import json
# import re
# import sys
# from pathlib import Path
# from typing import Any


# # ── Column-name mapping ────────────────────────────────────────────────────────
# #
# # Each entry maps:   raw_pdf_column_name  →  mysql_column_name
# #
# # Keys are stripped/normalised before lookup (see _norm_key).
# # Tables not listed here have no column renames needed.

# _COLUMN_MAP: dict[str, dict[str, str]] = {

#     "amount_summary": {
#         "S No.":   "s_no",
#         "":        "description",   # empty-string header = description column
#         "Amount":  "amount",
#     },

#     "complaint_meta": {
#         # complaint_meta uses a flat key→value dict, not a rows list.
#         # Keys are remapped by _remap_complaint_meta().
#         "Complaint Accepted By":   "complaint_accepted_by",
#         "Complaint Accepted Date": "complaint_accepted_date",
#         "Current Status":          "current_status",
#         "Under Process":           "under_process_date",
#     },

#     "complaint_transactions": {
#         "S No":                        "s_no",
#         "Account No./ Wallet ID":      "account_wallet_id",
#         "Transacti on ID":             "transaction_id",
#         "Card Details":                "card_details",
#         "Transacti on Amount":         "transaction_amount",
#         "Reference No.":               "reference_no",
#         "Transacti on Date & Time":    "transaction_datetime",
#         "Complaint Date":              "complaint_date",
#         "Bank/FIs":                    "bank_fi",
#     },

#     "failed_transactions": {
#         "S No.":                    "s_no",
#         "Account No.":              "account_no",
#         "Date":                     "transaction_date",
#         "Transaction Amount":       "transaction_amount",
#         "Reference No / Remarks":   "reference_remarks",
#         "Action Taken By":          "action_taken_by",
#         "Date of Action":           "date_of_action",
#     },

#     "hold_accounts": {
#         "S No.":                    "s_no",
#         "Account No.":              "account_no",
#         "Put on hold Date":         "hold_date",
#         "Put on hold Amount":       "hold_amount",
#         "Reference No / Remarks":   "reference_remarks",
#         "Action Taken By":          "action_taken_by",
#         "Date of Action":           "date_of_action",
#     },

#     # lien_transactions has extra splitting logic — see _remap_lien_row()
#     "lien_transactions": {
#         "S No.":                        "s_no",
#         "Bank /FIs":                    "bank_fi",
#         # "Account No./IFSC Code" is SPLIT → account_no + ifsc_code + layer
#         "Transac tion ID / UTR Number": "transaction_id",
#         "Transac tion Date & Time":     "transaction_datetime",
#         "Transacti on Amount":          "transaction_amount",
#         "Dispute d Amount":             "disputed_amount",
#         "Referen ce No / Remark s":     "reference_remarks",
#         "Action Taken By":              "action_taken_by",
#         "Date of Action":               "date_of_action",
#     },

#     "no_action_references": {
#         "S No.":                    "s_no",
#         "Reference No / Remarks":   "reference_remarks",
#         "Action Taken By":          "action_taken_by",
#         "Date of Action":           "date_of_action",
#     },

#     "pending_transactions": {
#         "S No.":                        "s_no",
#         "Bank":                         "bank",
#         "No. of Transaction Pending":   "no_of_transactions_pending",
#         "Amount Pending":               "amount_pending",
#         "Pending From":                 "pending_from",
#     },
# }


# # ── Normalisation helper ───────────────────────────────────────────────────────

# def _norm_key(raw: str) -> str:
#     """
#     Collapse runs of whitespace (including newlines from pdfplumber) to a
#     single space and strip leading/trailing whitespace.

#     e.g. "Transacti on ID"  →  "Transacti on ID"   (already single-spaced)
#          "Reference No /\nRemarks"  →  "Reference No / Remarks"
#     """
#     return re.sub(r"\s+", " ", raw).strip()


# # ── lien_transactions account/IFSC/layer splitter ─────────────────────────────

# # IFSC codes are exactly 11 characters: 4 alpha + 0 + 6 alphanumeric.
# # In practice the PDF sometimes omits the zero, so we match 4+ alpha chars
# # followed by word chars to identify the IFSC token.
# _IFSC_RE = re.compile(r"\b([A-Z]{4}[A-Z0-9]{2,8})\b")
# _LAYER_RE = re.compile(r"[Ll]ayer\s*:\s*(\d+)")


# def _split_account_ifsc_layer(raw_cell: str) -> tuple[str, str, str]:
#     """
#     Parse the combined "Account No./IFSC Code" cell from lien_transactions.

#     Examples
#     --------
#     "876005973 IPOS00001 Layer :1"
#         → account_no="876005973", ifsc_code="IPOS00001", layer="1"

#     "9142500051 YESB0PTMUPI Layer :5"
#         → account_no="9142500051", ifsc_code="YESB0PTMUPI", layer="5"

#     "5830017208 Layer :4"           (no IFSC in this row)
#         → account_no="5830017208", ifsc_code="", layer="4"

#     Returns (account_no, ifsc_code, layer_str)
#     """
#     cell = raw_cell.strip()

#     # Extract layer number
#     layer_match = _LAYER_RE.search(cell)
#     layer_str = layer_match.group(1) if layer_match else ""
#     # Remove "Layer :N" from cell so it doesn't pollute account/ifsc
#     cell_no_layer = _LAYER_RE.sub("", cell).strip()

#     # Extract IFSC code (first all-caps alphanumeric token that looks like IFSC)
#     ifsc_match = _IFSC_RE.search(cell_no_layer)
#     if ifsc_match:
#         ifsc_code = ifsc_match.group(1)
#         # Remove IFSC token from the remaining string to isolate account_no
#         account_no = cell_no_layer[:ifsc_match.start()].strip()
#     else:
#         ifsc_code = ""
#         account_no = cell_no_layer.strip()

#     return account_no, ifsc_code, layer_str


# # ── Per-row remapping helpers ──────────────────────────────────────────────────

# def _remap_row(row: dict[str, Any], col_map: dict[str, str]) -> dict[str, Any]:
#     """
#     Return a new dict with keys renamed according to col_map.
#     Keys not found in col_map are dropped (they don't map to a MySQL column).
#     """
#     new_row: dict[str, Any] = {}
#     for raw_key, value in row.items():
#         normalised = _norm_key(raw_key)
#         mysql_col = col_map.get(normalised)
#         if mysql_col is not None:
#             new_row[mysql_col] = value
#     return new_row


# def _remap_lien_row(row: dict[str, Any]) -> dict[str, Any]:
#     """
#     Like _remap_row but also splits the combined account/IFSC/layer cell.
#     """
#     col_map = _COLUMN_MAP["lien_transactions"]
#     new_row: dict[str, Any] = {}

#     for raw_key, value in row.items():
#         normalised = _norm_key(raw_key)

#         # Special case: split the combined cell
#         if normalised == "Account No./IFSC Code":
#             account_no, ifsc_code, layer_str = _split_account_ifsc_layer(str(value))
#             new_row["account_no"] = account_no
#             new_row["ifsc_code"] = ifsc_code
#             new_row["layer"] = layer_str
#             continue

#         mysql_col = col_map.get(normalised)
#         if mysql_col is not None:
#             new_row[mysql_col] = value

#     return new_row


# def _remap_complaint_meta(data: dict[str, Any]) -> dict[str, Any]:
#     """
#     complaint_meta has a flat key→value dict instead of a rows list.
#     Remap its keys using the same _COLUMN_MAP entry.
#     """
#     col_map = _COLUMN_MAP["complaint_meta"]
#     return {
#         col_map[_norm_key(k)]: v
#         for k, v in data.items()
#         if _norm_key(k) in col_map
#     }


# # ── Per-table dispatcher ───────────────────────────────────────────────────────

# def _remap_table(table_name: str, payload: dict[str, Any]) -> dict[str, Any]:
#     """
#     Given the raw JSON payload for *table_name*, return a new payload with
#     all column names remapped to MySQL names.

#     The JSON structure is preserved (same top-level keys); only the
#     'columns' list and the keys inside each 'rows' dict are changed.
#     complaint_meta uses a 'data' dict instead of 'rows'.
#     """
#     if table_name == "complaint_meta":
#         raw_data = payload.get("data", {})
#         return {
#             "table": table_name,
#             "data": _remap_complaint_meta(raw_data),
#         }

#     col_map = _COLUMN_MAP.get(table_name, {})
#     raw_rows: list[dict] = payload.get("rows", [])

#     if table_name == "lien_transactions":
#         remapped_rows = [_remap_lien_row(row) for row in raw_rows]
#         # Build new column list from the first remapped row
#         new_columns = list(remapped_rows[0].keys()) if remapped_rows else []
#     else:
#         remapped_rows = [_remap_row(row, col_map) for row in raw_rows]
#         # Remap the top-level 'columns' list as well
#         new_columns = [
#             col_map.get(_norm_key(c), _norm_key(c))
#             for c in payload.get("columns", [])
#         ]

#     return {
#         "table":           table_name,
#         "columns":         new_columns,
#         "rows":            remapped_rows,
#         "extraction_meta": payload.get("extraction_meta"),
#     }


# # ── Public API ─────────────────────────────────────────────────────────────────

# _TABLE_FILES: dict[str, str] = {
#     "amount_summary":         "amount_summary.json",
#     "complaint_meta":         "complaint_meta.json",
#     "complaint_transactions": "complaint_transactions.json",
#     "failed_transactions":    "failed_transactions.json",
#     "hold_accounts":          "hold_accounts.json",
#     "lien_transactions":      "lien_transactions.json",
#     "no_action_references":   "no_action_references.json",
#     "pending_transactions":   "pending_transactions.json",
# }


# def remap_tables(
#     output_dir: str | Path,
#     dry_run: bool = False,
# ) -> dict[str, dict]:
#     """
#     Read every JSON file in *output_dir*, remap column names to MySQL names,
#     and write the cleaned JSON back to the same file (in-place overwrite).

#     Parameters
#     ----------
#     output_dir : str | Path
#         Directory containing the raw JSON files produced by extractor.py.
#     dry_run : bool
#         If True, return the remapped payloads without writing to disk.

#     Returns
#     -------
#     dict[str, dict]
#         { table_name: remapped_payload }  for every table that was processed.

#     Raises
#     ------
#     FileNotFoundError
#         If *output_dir* does not exist.
#     """
#     output_dir = Path(output_dir)
#     if not output_dir.exists():
#         raise FileNotFoundError(f"output_dir not found: {output_dir}")

#     result: dict[str, dict] = {}

#     for table_name, filename in _TABLE_FILES.items():
#         path = output_dir / filename
#         if not path.exists():
#             print(f"  [!]  {table_name}: file not found — skipped ({path})")
#             continue

#         with open(path, encoding="utf-8") as f:
#             raw_payload = json.load(f)

#         remapped = _remap_table(table_name, raw_payload)
#         result[table_name] = remapped

#         if not dry_run:
#             with open(path, "w", encoding="utf-8") as f:
#                 json.dump(remapped, f, ensure_ascii=False, indent=2)
#             print(f"  [OK] {table_name}: columns remapped -> {path.name}")
#         else:
#             print(f"  [DRY] {table_name}: would remap {len(remapped.get('rows', remapped.get('data', {})))} row(s)")

#     return result


# # ── Standalone runner ──────────────────────────────────────────────────────────

# if __name__ == "__main__":
#     output_dir = sys.argv[1] if len(sys.argv) > 1 else "output"
#     print(f"\nRemapping columns in: {output_dir}\n" + "-" * 50)
#     remap_tables(output_dir)
#     print("\nDone.")






"""
Schema mapping - normalized JSON -> MySQL column names/types
==============================================================

Stage 3 of the pipeline. Takes the output of ``normalization.py`` and
reshapes it to match the given MySQL DDL exactly: column names become the
snake_case names the CREATE TABLE statements use, and values are cast to
strings that will actually load into DECIMAL / DATE / DATETIME / INT
columns (MySQL will reject "Rs. 38,983.54" into a DECIMAL, or
"11/08/2026 13:15:PM" into a DATETIME, as-is).

Unlike normalization.py, this stage IS allowed to add/remove/split fields,
because the target schema itself does - most notably
``lien_transactions``, where the schema splits the single
``Account No./IFSC Code`` text into three real columns:
``account_no``, ``ifsc_code``, ``layer``.

Per-table column maps
----------------------
Defined in TABLE_SCHEMAS below, each entry is
``(target_column, source_column, cast)`` where *cast* is one of:
``"str"``, ``"int"``, ``"decimal"``, ``"date"``, ``"datetime"``.
``lien_transactions`` additionally uses a custom splitter for the
combined account/IFSC/layer field (see ``_split_account_ifsc_layer``).

Value casting rules
--------------------
* ``"decimal"``  - strips ``Rs.``/``₹`` and thousands separators, e.g.
  ``"Rs. 38,983.54"`` -> ``"38983.54"``. Empty input -> JSON ``null``
  (not ``""`` - an empty string is not valid DECIMAL input for MySQL).
* ``"int"``      - digits only. Empty input -> JSON ``null``.
* ``"date"``     - ``"11/08/2026"`` -> ``"2026-08-11"``.
* ``"datetime"`` - handles three time shapes actually present in this
  data (see ``normalize_datetime_str``):
    1. the buggy colon-attached marker, ``"22:00:PM"`` (already 24-hour;
       the AM/PM is decorative and is dropped, not used for conversion)
    2. a real 12-hour clock with seconds, ``"11:06:35 AM"`` (converted)
    3. a bare 24-hour clock, ``"00:05:01"`` (passed through)
  Unparseable input is left as the original string and flagged in the
  ``_schema_mapping`` report rather than silently dropped.
* ``"str"``      - light whitespace cleanup only.

Account/IFSC/Layer split (lien_transactions only)
---------------------------------------------------
Source values look like ``"<account> <ifsc> Layer :<n>"``. Token count
after stripping the ``Layer :n`` suffix decides confidence:
  * 1 token  -> account_no = token, ifsc_code = null              (certain)
  * 2 tokens -> account_no, ifsc_code = the two tokens             (certain)
  * 3 tokens -> account_no = token 1, ifsc_code = tokens 2+3 joined
               (the IFSC got wrap-split, e.g. "YESB0PTMU PI")      (ambiguous, flagged)
  * other    -> account_no = token 1, ifsc_code = null, row flagged for
               manual review (e.g. a UPI wallet ID with no real IFSC)

Usage (standalone):
    python schema_mapping.py <normalized_dir> [output_dir]

    If output_dir is omitted, files are rewritten in place.

    python schema_mapping.py --self-test
"""

import copy
import json
import re
import sys
from pathlib import Path


# ── Per-table column maps: (target_column, source_column, cast) ───────────────

TABLE_SCHEMAS = {
    "amount_summary": [
        ("s_no",        "S No.", "int"),
        ("description", "",      "str"),
        ("amount",      "Amount", "decimal"),
    ],
    "complaint_transactions": [
        ("s_no",                 "S No",                        "int"),
        ("account_wallet_id",    "Account No./Wallet ID",       "str"),
        ("transaction_id",       "Transaction ID",               "str"),
        ("card_details",         "Card Details",                 "str"),
        ("transaction_amount",   "Transaction Amount",           "decimal"),
        ("reference_no",         "Reference No.",                "str"),
        ("transaction_datetime", "Transaction Date & Time",      "datetime"),
        ("complaint_date",       "Complaint Date",               "datetime"),
        ("bank_fi",              "Bank/FIs",                     "str"),
    ],
    "failed_transactions": [
        ("s_no",               "S No.",                   "int"),
        ("account_no",         "Account No.",             "str"),
        ("transaction_date",   "Date",                    "date"),
        ("transaction_amount", "Transaction Amount",      "decimal"),
        ("reference_remarks",  "Reference No/Remarks",    "str"),
        ("action_taken_by",    "Action Taken By",         "str"),
        ("date_of_action",     "Date of Action",          "datetime"),
    ],
    "hold_accounts": [
        ("s_no",              "S No.",                 "int"),
        ("account_no",        "Account No.",           "str"),
        ("hold_date",         "Put on hold Date",      "date"),
        ("hold_amount",       "Put on hold Amount",    "decimal"),
        ("reference_remarks", "Reference No/Remarks",  "str"),
        ("action_taken_by",   "Action Taken By",       "str"),
        ("date_of_action",    "Date of Action",        "datetime"),
    ],
    "no_action_references": [
        ("s_no",              "S No.",                 "int"),
        ("reference_remarks", "Reference No/Remarks",  "str"),
        ("action_taken_by",   "Action Taken By",       "str"),
        ("date_of_action",    "Date of Action",        "datetime"),
    ],
    "pending_transactions": [
        ("s_no",                       "S No.",                        "int"),
        ("bank",                       "Bank",                         "str"),
        ("no_of_transactions_pending", "No. of Transaction Pending",   "int"),
        ("amount_pending",             "Amount Pending",               "decimal"),
        ("pending_from",               "Pending From",                 "datetime"),
    ],
    # lien_transactions is handled by a dedicated function because of the
    # account/IFSC/layer split - see map_lien_transactions().
}

# complaint_meta (key/value payload, not a row table)
META_SCHEMA = [
    ("complaint_accepted_by",   "Complaint Accepted By",   "str"),
    ("complaint_accepted_date", "Complaint Accepted Date", "datetime"),
    ("current_status",          "Current Status",          "str"),
    ("under_process_date",      "Under Process",           "datetime"),
]


# ── Value casting ──────────────────────────────────────────────────────────────

def cast_str(val):
    val = (val or "").strip()
    return val if val else ""


def cast_int(val):
    val = (val or "").strip()
    if not val:
        return None
    digits = re.sub(r"[^\d]", "", val)
    return int(digits) if digits else None


_AMOUNT_JUNK_RE = re.compile(r"[Rr]s\.?|₹|,")


def cast_decimal(val):
    val = (val or "").strip()
    if not val:
        return None
    cleaned = _AMOUNT_JUNK_RE.sub("", val).strip()
    if not cleaned:
        return None
    try:
        return f"{float(cleaned):.2f}"
    except ValueError:
        return None


_DATE_RE = re.compile(r"^(\d{2})/(\d{2})/(\d{4})\s*(.*)$")


def cast_date(val):
    val = (val or "").strip()
    if not val:
        return None
    m = _DATE_RE.match(val)
    if not m:
        return None
    dd, mm, yyyy, _rest = m.groups()
    return f"{yyyy}-{mm}-{dd}"


def cast_datetime(val):
    """
    Normalize the three time shapes seen in this dataset to MySQL
    'YYYY-MM-DD HH:MM:SS'. Returns None on empty input, and the original
    string (unchanged) when the shape isn't recognized, so a caller can
    detect and flag it rather than silently lose the value.
    """
    val = (val or "").strip()
    if not val:
        return None
    m = _DATE_RE.match(val)
    if not m:
        return val          # unparseable date part - flagged by caller
    dd, mm, yyyy, rest = m.groups()
    date_str = f"{yyyy}-{mm}-{dd}"
    rest = rest.strip()
    if not rest:
        return f"{date_str} 00:00:00"

    # Shape 1: buggy colon-attached marker - "22:00:PM", "00:00:AM".
    # This data is already 24-hour; the AM/PM here is decorative junk
    # left over from a template and must NOT be used to shift the hour.
    m1 = re.match(r"^(\d{1,2}):(\d{2}):(AM|PM)$", rest, re.I)
    if m1:
        h, mi, _ = m1.groups()
        return f"{date_str} {int(h):02d}:{mi}:00"

    # Shape 2: real 12-hour clock with seconds - "11:06:35 AM".
    m2 = re.match(r"^(\d{1,2}):(\d{2}):(\d{2})\s*(AM|PM)?$", rest, re.I)
    if m2:
        h, mi, s, ampm = m2.groups()
        h = int(h)
        if ampm:
            ampm = ampm.upper()
            if ampm == "PM" and h != 12:
                h += 12
            if ampm == "AM" and h == 12:
                h = 0
        return f"{date_str} {h:02d}:{mi}:{s}"

    # Shape 3: "HH:MM" or "HH:MM AM/PM", no seconds.
    m3 = re.match(r"^(\d{1,2}):(\d{2})\s*(AM|PM)?$", rest, re.I)
    if m3:
        h, mi, ampm = m3.groups()
        h = int(h)
        if ampm:
            ampm = ampm.upper()
            if ampm == "PM" and h != 12:
                h += 12
            if ampm == "AM" and h == 12:
                h = 0
        return f"{date_str} {h:02d}:{mi}:00"

    return val               # unrecognized time shape - flagged by caller


_CASTS = {
    "str": cast_str, "int": cast_int, "decimal": cast_decimal,
    "date": cast_date, "datetime": cast_datetime,
}


# ── lien_transactions: account/IFSC/layer split ────────────────────────────────

_LAYER_RE = re.compile(r"^(.*?)\s+Layer\s*:\s*(\d+)\s*$")


def _split_account_ifsc_layer(raw: str):
    """
    Return (account_no, ifsc_code, layer, flag) where flag is one of
    "certain", "reconstructed" (wrap-split IFSC rejoined), or
    "ambiguous" (doesn't fit the account+IFSC shape at all).

    >>> _split_account_ifsc_layer("876005973 IPOS00001 Layer :1")
    ('876005973', 'IPOS00001', 1, 'certain')
    >>> _split_account_ifsc_layer("5830017208 Layer :4")
    ('5830017208', None, 4, 'certain')
    >>> _split_account_ifsc_layer("9142500051 YESB0PTMU PI Layer :5")
    ('9142500051', 'YESB0PTMUPI', 5, 'reconstructed')
    >>> _split_account_ifsc_layer("AHBASHZS3 72RA ZCEDU5DQK BGQ P6Q PPIW884207 Layer :3")[3]
    'ambiguous'
    """
    raw = (raw or "").strip()
    m = _LAYER_RE.match(raw)
    if not m:
        return (raw or None), None, None, "ambiguous"
    prefix, layer = m.groups()
    tokens = prefix.split()
    layer_int = int(layer)

    if len(tokens) == 1:
        return tokens[0], None, layer_int, "certain"
    if len(tokens) == 2:
        return tokens[0], tokens[1], layer_int, "certain"
    if len(tokens) == 3:
        return tokens[0], "".join(tokens[1:]), layer_int, "reconstructed"
    return tokens[0], None, layer_int, "ambiguous"


def map_lien_transactions(rows: list) -> tuple:
    """Return (new_rows, flagged) for lien_transactions."""
    new_rows = []
    flagged = []
    for row in rows:
        s_no = cast_int(row.get("S No."))
        account_no, ifsc_code, layer, flag = _split_account_ifsc_layer(
            row.get("Account No./IFSC Code")
        )
        new_row = {
            "s_no": s_no,
            "bank_fi": cast_str(row.get("Bank/FIs")),
            "account_no": account_no,
            "ifsc_code": ifsc_code,
            "layer": layer,
            "transaction_id": cast_str(row.get("Transaction ID/UTR Number")),
            "transaction_datetime": cast_datetime(row.get("Transaction Date & Time")),
            "transaction_amount": cast_decimal(row.get("Transaction Amount")),
            "disputed_amount": cast_decimal(row.get("Disputed Amount")),
            "reference_remarks": cast_str(row.get("Reference No/Remarks")),
            "action_taken_by": cast_str(row.get("Action Taken By")),
            "date_of_action": cast_datetime(row.get("Date of Action")),
        }
        new_rows.append(new_row)
        if flag != "certain":
            flagged.append({"s_no": s_no, "reason": flag,
                            "source_value": row.get("Account No./IFSC Code")})
    return new_rows, flagged


# ── Generic table mapping ───────────────────────────────────────────────────────

def map_table(payload: dict) -> dict:
    """
    Map one normalized payload to its MySQL schema shape. Returns a new
    payload: ``{"table", "columns", "rows", "_schema_mapping"}`` for row
    tables, or ``{"table", "data", "_schema_mapping"}`` for complaint_meta.
    """
    payload = copy.deepcopy(payload)
    table = payload.get("table", "<unknown>")

    if table == "complaint_meta" or ("data" in payload and "rows" not in payload):
        data = payload.get("data") or {}
        new_data = {}
        unparsed = []
        for target, source, cast in META_SCHEMA:
            raw = data.get(source, "")
            val = _CASTS[cast](raw)
            if cast == "datetime" and val is not None and raw and val == raw \
               and not re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$", val):
                unparsed.append({"field": target, "raw": raw})
            new_data[target] = val
        return {
            "table": "complaint_meta",
            "data": new_data,
            "_schema_mapping": {
                "kind": "key_value",
                "unparsed_datetimes": unparsed,
                "status": "ok" if not unparsed else "needs_review",
            },
        }

    rows = payload.get("rows", [])

    if table == "lien_transactions":
        new_rows, flagged = map_lien_transactions(rows)
        columns = ["s_no", "bank_fi", "account_no", "ifsc_code", "layer",
                   "transaction_id", "transaction_datetime",
                   "transaction_amount", "disputed_amount",
                   "reference_remarks", "action_taken_by", "date_of_action"]
        return {
            "table": table,
            "columns": columns,
            "rows": new_rows,
            "_schema_mapping": {
                "kind": "rows",
                "rows": len(new_rows),
                "flagged_rows": flagged,
                "status": "ok" if not flagged else "needs_review",
            },
        }

    schema = TABLE_SCHEMAS.get(table)
    if schema is None:
        # Unknown table - pass through untouched rather than guess.
        payload["_schema_mapping"] = {
            "kind": "unmapped",
            "status": "skipped",
            "reason": f"no schema defined for table '{table}'",
        }
        return payload

    columns = [target for target, _src, _cast in schema]
    unparsed = []
    new_rows = []
    for row in rows:
        new_row = {}
        for target, source, cast in schema:
            raw = row.get(source, "")
            val = _CASTS[cast](raw)
            if cast == "datetime" and val is not None and raw and val == raw \
               and not re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$", str(val)):
                unparsed.append({
                    "s_no": row.get(_id_source(schema)), "field": target, "raw": raw
                })
            new_row[target] = val
        new_rows.append(new_row)

    return {
        "table": table,
        "columns": columns,
        "rows": new_rows,
        "_schema_mapping": {
            "kind": "rows",
            "rows": len(new_rows),
            "unparsed_datetimes": unparsed,
            "status": "ok" if not unparsed else "needs_review",
        },
    }


def _id_source(schema):
    for target, source, _cast in schema:
        if target == "s_no":
            return source
    return None


# ── Directory orchestration ─────────────────────────────────────────────────────

def map_directory(input_dir, output_dir=None, progress=None) -> dict:
    input_dir = Path(input_dir)
    if not input_dir.is_dir():
        raise NotADirectoryError(f"Not a directory: {input_dir}")
    output_dir = input_dir if output_dir is None else Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    written = {}
    for path in sorted(input_dir.glob("*.json")):
        with open(path, encoding="utf-8") as f:
            try:
                payload = json.load(f)
            except json.JSONDecodeError:
                print(f"  [schema_map] SKIP (not valid JSON): {path.name}")
                continue
        if not isinstance(payload, dict) or "table" not in payload:
            print(f"  [schema_map] SKIP (not a table payload): {path.name}")
            continue

        result = map_table(payload)
        out_path = output_dir / path.name
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)

        report = result["_schema_mapping"]
        written[result["table"]] = out_path
        if progress:
            progress(f"Mapped {result['table'].replace('_', ' ')} to the database schema.")
        flag = "" if report["status"] == "ok" else f"  <-- {report['status'].upper()}"
        if report["kind"] == "rows":
            print(f"  [schema_map] {result['table']:<24} "
                  f"{report['rows']} row(s), "
                  f"{len(result['columns'])} column(s){flag}")
        elif report["kind"] == "key_value":
            print(f"  [schema_map] {result['table']:<24} "
                  f"{len(result['data'])} field(s){flag}")
        else:
            print(f"  [schema_map] {result['table']:<24} SKIPPED - {report['reason']}")
    return written


# ── Tests ─────────────────────────────────────────────────────────────────────

def _run_tests():
    import doctest

    PASS, FAIL = "\033[32mPASS\033[0m", "\033[31mFAIL\033[0m"
    results = []

    def check(name, ok, detail=""):
        results.append((name, ok, detail))
        line = f"  [{PASS if ok else FAIL}] {name}"
        if not ok:
            line += f"\n         {detail}"
        print(line)

    print("=" * 64); print("  doctests"); print("=" * 64)
    dt = doctest.testmod(sys.modules[__name__], verbose=False)
    check(f"doctests ({dt.attempted} run)", dt.failed == 0, f"{dt.failed} failures")

    print("\n" + "=" * 64); print("  cast_decimal"); print("=" * 64)
    check("'Rs. 38,983.54' -> '38983.54'", cast_decimal("Rs. 38,983.54") == "38983.54")
    check("'500.00' -> '500.00'", cast_decimal("500.00") == "500.00")
    check("'' -> None", cast_decimal("") is None)
    check("'Rs. 0.10' -> '0.10'", cast_decimal("Rs. 0.10") == "0.10")

    print("\n" + "=" * 64); print("  cast_date / cast_int"); print("=" * 64)
    check("'11/08/2026' -> '2026-08-11'", cast_date("11/08/2026") == "2026-08-11")
    check("'' -> None (date)", cast_date("") is None)
    check("'1' -> 1 (int)", cast_int("1") == 1)
    check("'' -> None (int)", cast_int("") is None)

    print("\n" + "=" * 64); print("  cast_datetime - three real shapes"); print("=" * 64)
    check("buggy colon-AM/PM: '29/06/2026 22:00:PM'",
          cast_datetime("29/06/2026 22:00:PM") == "2026-06-29 22:00:00",
          cast_datetime("29/06/2026 22:00:PM"))
    check("buggy colon-AM/PM midnight: '08/08/2026 00:00:AM'",
          cast_datetime("08/08/2026 00:00:AM") == "2026-08-08 00:00:00")
    check("real 12hr+seconds: '12/08/2026 11:06:35 AM'",
          cast_datetime("12/08/2026 11:06:35 AM") == "2026-08-12 11:06:35",
          cast_datetime("12/08/2026 11:06:35 AM"))
    check("real 12hr+seconds PM rollover: '12/08/2026 01:06:35 PM'",
          cast_datetime("12/08/2026 01:06:35 PM") == "2026-08-12 13:06:35",
          cast_datetime("12/08/2026 01:06:35 PM"))
    check("bare 24hr+seconds: '12/08/2026 00:05:01'",
          cast_datetime("12/08/2026 00:05:01") == "2026-08-12 00:05:01")
    check("empty -> None", cast_datetime("") is None)
    check("date only: '11/08/2026'",
          cast_datetime("11/08/2026") == "2026-08-11 00:00:00")

    print("\n" + "=" * 64); print("  lien account/IFSC/layer split"); print("=" * 64)
    check("2-token clean split",
          _split_account_ifsc_layer("38501960 KKBK00946 Layer :2")
          == ("38501960", "KKBK00946", 2, "certain"))
    check("1-token, no IFSC",
          _split_account_ifsc_layer("5830017208 Layer :4")
          == ("5830017208", None, 4, "certain"))
    check("3-token wrap-split IFSC rejoined",
          _split_account_ifsc_layer("9142500051 YESB0PTMU PI Layer :5")
          == ("9142500051", "YESB0PTMUPI", 5, "reconstructed"))
    ambiguous = _split_account_ifsc_layer(
        "AHBASHZS3 72RA ZCEDU5DQK BGQ P6Q PPIW884207 Layer :3")
    check("wallet-id case flagged ambiguous", ambiguous[3] == "ambiguous")

    print("\n" + "=" * 64); print("  map_table - amount_summary"); print("=" * 64)
    payload = {
        "table": "amount_summary",
        "columns": ["S No.", "", "Amount"],
        "rows": [
            {"S No.": "1", "": "Transaction put on hold", "Amount": "Rs. 38,983.54"},
            {"S No.": "2", "": "Other", "Amount": "Rs. 0.10"},
        ],
    }
    out = map_table(payload)
    check("columns match schema",
          out["columns"] == ["s_no", "description", "amount"])
    check("amount cast to plain decimal string",
          out["rows"][0]["amount"] == "38983.54", out["rows"][0])
    check("s_no cast to int", out["rows"][0]["s_no"] == 1)

    print("\n" + "=" * 64)
    print("  map_table - lien_transactions (real defect rows)")
    print("=" * 64)
    lien_payload = {
        "table": "lien_transactions",
        "columns": ["S No.", "Bank/FIs", "Account No./IFSC Code",
                    "Transaction ID/UTR Number", "Transaction Date & Time",
                    "Transaction Amount", "Disputed Amount",
                    "Reference No/Remarks", "Action Taken By", "Date of Action"],
        "rows": [
            {"S No.": "1", "Bank/FIs": "India Post Payments Bank",
             "Account No./IFSC Code": "876005973 IPOS00001 Layer :1",
             "Transaction ID/UTR Number": "985045863457",
             "Transaction Date & Time": "11/08/2026 10:11:AM",
             "Transaction Amount": "500.00", "Disputed Amount": "500.00",
             "Reference No/Remarks": "Success", "Action Taken By": "Indian Bank",
             "Date of Action": "11/08/2026 13:25:01"},
            {"S No.": "18", "Bank/FIs": "AIRPAY",
             "Account No./IFSC Code": "5830017208 Layer :4",
             "Transaction ID/UTR Number": "994265602304",
             "Transaction Date & Time": "29/06/2026 00:00:AM",
             "Transaction Amount": "3,000.00", "Disputed Amount": "2,643.20",
             "Reference No/Remarks": "", "Action Taken By": "Ratnakar Bank Limited",
             "Date of Action": "12/08/2026 10:43:46"},
        ],
    }
    lien_out = map_table(lien_payload)
    check("columns include split account/ifsc/layer",
          lien_out["columns"] == ["s_no", "bank_fi", "account_no", "ifsc_code",
                                   "layer", "transaction_id", "transaction_datetime",
                                   "transaction_amount", "disputed_amount",
                                   "reference_remarks", "action_taken_by",
                                   "date_of_action"])
    check("row 1 split correctly",
          (lien_out["rows"][0]["account_no"], lien_out["rows"][0]["ifsc_code"],
           lien_out["rows"][0]["layer"]) == ("876005973", "IPOS00001", 1))
    check("row 18 (no ifsc) split correctly",
          (lien_out["rows"][1]["account_no"], lien_out["rows"][1]["ifsc_code"],
           lien_out["rows"][1]["layer"]) == ("5830017208", None, 4))
    check("no rows flagged for these two clean rows",
          lien_out["_schema_mapping"]["flagged_rows"] == [])

    print("\n" + "=" * 64)
    print("  map_table - complaint_meta")
    print("=" * 64)
    meta_out = map_table({
        "table": "complaint_meta",
        "data": {
            "Complaint Accepted By": "Login Id: nccrp.aurangabadr",
            "Complaint Accepted Date": "12/08/2026 11:06:35 AM",
            "Current Status": "Under Process",
            "Under Process": "12/08/2026 11:06:35 AM",
        },
    })
    check("keys renamed to schema",
          set(meta_out["data"].keys()) == {"complaint_accepted_by",
                                            "complaint_accepted_date",
                                            "current_status",
                                            "under_process_date"})
    check("datetime converted",
          meta_out["data"]["complaint_accepted_date"] == "2026-08-12 11:06:35",
          meta_out["data"]["complaint_accepted_date"])

    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print(f"\n{'=' * 64}")
    print(f"  {passed}/{total} checks passed"
          + ("  \u2713" if passed == total else "  \u2717  - see FAIL lines"))
    print("=" * 64)
    sys.exit(0 if passed == total else 1)


def main():
    args = sys.argv[1:]
    if args and args[0] in ("--self-test", "--test"):
        _run_tests()
        return
    if not args:
        print("Usage: python schema_mapping.py <normalized_dir> [output_dir]")
        print("       python schema_mapping.py --self-test")
        sys.exit(1)
    input_dir = args[0]
    output_dir = args[1] if len(args) >= 2 else None
    print(f"Mapping normalized JSON to MySQL schema in: {input_dir}")
    written = map_directory(input_dir, output_dir)
    print(f"\nMapped {len(written)} table(s):")
    for name, path in written.items():
        print(f"  [{name}]  ->  {path}")


if __name__ == "__main__":
    main()
