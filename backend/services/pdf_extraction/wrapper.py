# """
# Pipeline: extractor.py -> normalization.py
# ===========================================

# Runs the two stages back-to-back so a single command takes a PDF all the
# way to normalized JSON. Neither ``extractor.py`` nor ``normalization.py``
# is modified - this file just imports and calls them in order.

# Stage 1 (extractor.extract_tables) writes raw JSON to <raw_dir>.
# Stage 2 (normalization.normalize_directory) reads <raw_dir> and writes
# normalized JSON to <normalized_dir>, leaving <raw_dir> untouched so you
# can always diff against the raw extractor output.

# Usage:
#     python run_pipeline.py <path_to_pdf> [output_root]

#     output_root defaults to a folder called `pipeline_output` placed next
#     to the PDF. Inside it you get:

#         pipeline_output/
#           raw/          <- extractor.py's output, unmodified
#           normalized/   <- normalization.py's output

# Exit code is non-zero if any table's normalization report recommends
# escalation (escalate_to_camelot / escalate_to_vlm), so this can be used
# as a CI gate.
# """

# import sys
# import json
# from pathlib import Path

# from extractor import extract_tables
# from normalization import normalize_directory


# def run_pipeline(pdf_path, output_root=None) -> dict:
#     pdf_path = Path(pdf_path)
#     if not pdf_path.exists():
#         raise FileNotFoundError(f"PDF not found: {pdf_path}")

#     if output_root is None:
#         output_root = pdf_path.parent / "pipeline_output"
#     output_root = Path(output_root)

#     # raw_dir = output_root / "raw"
#     # normalized_dir = output_root / "normalized"
#     raw_dir = output_root
#     normalized_dir = output_root

#     print("=" * 64)
#     print("STAGE 1/2 - extraction")
#     print("=" * 64)
#     extracted = extract_tables(pdf_path, raw_dir)
#     print(f"\nExtracted {len(extracted)} table(s) -> {raw_dir}")

#     print("\n" + "=" * 64)
#     print("STAGE 2/2 - normalization")
#     print("=" * 64)
#     normalized = normalize_directory(raw_dir, normalized_dir)
#     print(f"\nNormalized {len(normalized)} table(s) -> {normalized_dir}")

#     escalations = _collect_escalations(normalized_dir)
#     if escalations:
#         print("\n" + "=" * 64)
#         print("ESCALATIONS - needs a human / VLM / Camelot pass")
#         print("=" * 64)
#         for table, action in escalations.items():
#             print(f"  [{table}]  {action}")
#     else:
#         print("\nNo escalations - every table normalized cleanly.")

#     return {
#         "raw_dir": raw_dir,
#         "normalized_dir": normalized_dir,
#         "extracted": extracted,
#         "normalized": normalized,
#         "escalations": escalations,
#     }


# def _collect_escalations(normalized_dir: Path) -> dict:
#     escalations = {}
#     for path in sorted(Path(normalized_dir).glob("*.json")):
#         with open(path, encoding="utf-8") as f:
#             payload = json.load(f)
#         report = payload.get("_normalization", {})
#         action = report.get("recommended_action", "no_action_needed")
#         if action not in ("no_action_needed", "merge_blank_rows_into_previous"):
#             escalations[payload.get("table", path.stem)] = action
#     return escalations


# def main():
#     if len(sys.argv) < 2:
#         print("Usage: python run_pipeline.py <path_to_pdf> [output_root]")
#         sys.exit(1)

#     pdf_path = sys.argv[1]
#     output_root = sys.argv[2] if len(sys.argv) >= 3 else None

#     result = run_pipeline(pdf_path, output_root)
#     sys.exit(1 if result["escalations"] else 0)


# if __name__ == "__main__":
#     main()




"""
Pipeline: extractor.py -> normalization.py -> schema_mapping.py
==================================================================

Runs all three stages back-to-back so a single command takes a PDF all
the way to MySQL-ready JSON. None of ``extractor.py``, ``normalization.py``
or ``schema_mapping.py`` is modified - this file just imports and calls
them in order.

All three stages write to the SAME directory (in place): extraction
writes raw JSON, normalization then overwrites each file with the
cleaned version, and schema_mapping overwrites again with the MySQL
column names/types. Only the final schema-mapped JSON survives on disk
per table - the raw and normalized intermediate versions are not kept.
If you want to diff against an earlier stage later, keep that in mind.

Stage 1 (extractor.extract_tables)          -> raw JSON
Stage 2 (normalization.normalize_directory) -> cleaned JSON (overwrites stage 1)
Stage 3 (schema_mapping.map_directory)      -> MySQL-shaped JSON (overwrites stage 2)

Usage:
    python run_pipeline.py <path_to_pdf> [output_root]

    output_root defaults to a folder called `pipeline_output` placed next
    to the PDF.

Exit code is non-zero if any table's normalization report recommends
escalation (escalate_to_camelot / escalate_to_vlm) OR any table's
schema-mapping report has rows/fields flagged for review, so this can be
used as a CI gate before a MySQL load.
"""

import sys
import json
from pathlib import Path
import argparse
from .extractor import extract_tables
from .normalization import normalize_directory
from .correct_columns import map_directory
from .commit_to_db import load_all
from ..auth import get_current_user
from fastapi import Request, HTTPException, status

def run_pipeline(request:Request, pdf_path, output_root=None) -> dict:
    pdf_path = Path(pdf_path)
    if not pdf_path.exists():
        raise FileNotFoundError(f"PDF not found: {pdf_path}")

    if output_root is None:
        output_root = pdf_path.parent / "pipeline_output"
    output_root = Path(output_root)

    raw_dir = output_root
    normalized_dir = output_root
    db_schema_dir = output_root

    print("=" * 64)
    print("STAGE 1/3 - extraction")
    print("=" * 64)
    extracted = extract_tables(pdf_path, raw_dir)
    print(f"\nExtracted {len(extracted)} table(s) -> {raw_dir}")

    print("\n" + "=" * 64)
    print("STAGE 2/3 - normalization")
    print("=" * 64)
    normalized = normalize_directory(raw_dir, normalized_dir)
    print(f"\nNormalized {len(normalized)} table(s) -> {normalized_dir}")

    # Snapshot the normalization report before stage 3 overwrites these
    # files, since escalation info lives in "_normalization" and stage 3
    # replaces that key with "_schema_mapping".
    escalations = _collect_escalations(normalized_dir)

    print("\n" + "=" * 64)
    print("STAGE 3/3 - schema mapping (MySQL column names/types)")
    print("=" * 64)
    mapped = map_directory(normalized_dir, db_schema_dir)
    print(f"\nMapped {len(mapped)} table(s) -> {db_schema_dir}")

    review_flags = _collect_review_flags(db_schema_dir)

    if escalations:
        print("\n" + "=" * 64)
        print("NORMALIZATION ESCALATIONS - needs a human / VLM / Camelot pass")
        print("=" * 64)
        for table, action in escalations.items():
            print(f"  [{table}]  {action}")
    if review_flags:
        print("\n" + "=" * 64)
        print("SCHEMA-MAPPING FLAGS - review before inserting into MySQL")
        print("=" * 64)
        for table, detail in review_flags.items():
            print(f"  [{table}]  {detail}")
    if not escalations and not review_flags:
        print("\nNo escalations or flags - every table mapped cleanly, ready to load.")

    print("Pushing to database")

    _COOKIE_NAME = "access_token"
    access_token = request.cookies.get(_COOKIE_NAME)
    
    if not access_token:
        raise ValueError("No access token provided.")
    
    try:
        user_id = get_current_user(access_token)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(e),
        )
    load_all(user_id, db_schema_dir)

    return {
        "raw_dir": raw_dir,
        "normalized_dir": normalized_dir,
        "db_schema_dir": db_schema_dir,
        "extracted": extracted,
        "normalized": normalized,
        "mapped": mapped,
        "escalations": escalations,
        "review_flags": review_flags,
    }


def _collect_escalations(normalized_dir: Path) -> dict:
    escalations = {}
    for path in sorted(Path(normalized_dir).glob("*.json")):
        with open(path, encoding="utf-8") as f:
            payload = json.load(f)
        report = payload.get("_normalization", {})
        action = report.get("recommended_action", "no_action_needed")
        if action not in ("no_action_needed", "merge_blank_rows_into_previous"):
            escalations[payload.get("table", path.stem)] = action
    return escalations


def _collect_review_flags(db_schema_dir: Path) -> dict:
    flags = {}
    for path in sorted(Path(db_schema_dir).glob("*.json")):
        with open(path, encoding="utf-8") as f:
            payload = json.load(f)
        report = payload.get("_schema_mapping", {})
        if report.get("status") not in ("ok", None):
            table = payload.get("table", path.stem)
            if report.get("kind") == "rows" and report.get("flagged_rows"):
                flags[table] = f"{len(report['flagged_rows'])} row(s) flagged"
            elif report.get("unparsed_datetimes"):
                flags[table] = f"{len(report['unparsed_datetimes'])} unparsed datetime(s)"
            else:
                flags[table] = report.get("status")
    return flags


def main():
    if len(sys.argv) < 2:
        print("Usage: python run_pipeline.py <path_to_pdf> [output_root]")
        sys.exit(1)

    pdf_path = sys.argv[1]
    output_root = sys.argv[2] if len(sys.argv) >= 3 else None

    result = run_pipeline(pdf_path, output_root)
    sys.exit(1 if (result["escalations"] or result["review_flags"]) else 0)


if __name__ == "__main__":
    main()