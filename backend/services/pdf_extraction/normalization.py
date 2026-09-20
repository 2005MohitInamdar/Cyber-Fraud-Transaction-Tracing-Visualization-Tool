"""
Normalization for extractor.py output
=====================================

Takes the JSON files written by ``extractor.py`` and repairs the text-layer
damage that pdfplumber leaves behind, **without adding or removing any
field**. Every table keeps exactly the keys it came in with; only the key
*spelling* and the cell *values* are edited.

Four classes of damage are repaired
-----------------------------------
1. **Split words** - a word broken by a column-wrap artifact::

       "Transac tion Date & Time"  ->  "Transaction Date & Time"
       "Referen ce No / Remark s"  ->  "Reference No/Remarks"
       "Fino Payment s Bank"       ->  "Fino Payments Bank"
       "Lien_Sta t us: No"         ->  "Lien_Status: No"

   This is vocabulary-driven, never a blind regex. ``"Account No."`` and
   ``"Date of Action"`` both *look* like split words to a naive pattern
   (a letter-run followed by a short letter-run); they are left alone
   because ``"accountno"`` and ``"dateof"`` are not words. A join only
   happens when the concatenation is in ``DOMAIN_VOCAB`` **and** at least
   one of the fragments is not itself a word.

2. **Split numbers** - a digit sequence broken mid-run::

       "11/08/202 6 10:11:AM"  ->  "11/08/2026 10:11:AM"
       "985045863 457"         ->  "985045863457"
       "01/07/20 26 11:59:AM"  ->  "01/07/2026 11:59:AM"

   Only applied to columns that hold structured numerics (dates, account
   numbers, UTR/IDs) so prose numbers in remark fields are never touched.

3. **Phantom rows** - pdfplumber emits a row whose id cell is blank and
   whose other cells hold the tail of the previous row. These are merged
   back into the row above and removed.

4. **Slash spacing** in column names - ``"Bank /FIs"`` -> ``"Bank/FIs"``.
   Controlled by the ``normalize_slashes`` flag.

Why the id column is resolved dynamically
-----------------------------------------
The extractor's ``_ID_COLUMNS`` map claims every table uses ``"S No."``,
but the header text actually printed in the PDF is not consistent:
``complaint_transactions`` uses ``"S No"`` with **no trailing dot**.
Hardcoding ``"S No."`` makes every row of that table look like a phantom
row, and all 6 rows silently collapse into 1. So the id column is resolved
from the row keys by shape (``"S No"``, ``"S No."``, ``"Sr No"``, ...),
not by name lookup.

Usage (standalone):
    python normalization.py <input_dir> [output_dir]

    <input_dir>  is the folder extractor.py wrote its JSON into.
    If <output_dir> is omitted the files are normalized **in place**.

    python normalization.py --self-test     # run the unit tests
"""

import copy
import difflib
import json
import re
import sys
import warnings
from pathlib import Path


# ── Domain vocabulary ─────────────────────────────────────────────────────────
#
# Used only to *confirm* a candidate join. Adding a word here makes the
# repairer able to fix that word; it can never cause unrelated text to be
# joined, because a join requires an exact vocabulary hit.

DOMAIN_VOCAB = frozenset({
    # generic table / banking nouns
    "transaction", "transactions", "transact", "transfer", "transferred",
    "reference", "references", "remark", "remarks", "amount", "amounts",
    "account", "accounts", "statement", "statements", "balance", "ending",
    "payment", "payments", "disputed", "dispute", "pending", "complaint",
    "details", "number", "numbers", "action", "actions", "taken", "date",
    "dates", "wallet", "card", "code", "status", "success", "successful",
    "failed", "money", "through", "credit", "debit", "lien", "marked",
    "hold", "holds", "layer", "utr", "ifsc", "upi", "total", "fraudulent",
    "victim", "supplier", "global", "merchant", "acknowledgement",
    # institution names that appear split across cells
    "bank", "banks", "india", "indian", "national", "punjab", "kotak",
    "mahindra", "axis", "paytm", "airtel", "ratnakar", "limited", "baroda",
    "fino", "jio", "nsdl", "hdfc", "sbin", "yes", "post", "payu", "indus",
    "shelter", "finance", "financ", "indiashelter", "airpay", "shreyas",
})

# Fragments that must never be treated as standalone words when deciding
# whether a join is warranted (so "Payment" + "s" still merges even though
# "payment" is itself a word).
_NEVER_STANDALONE = frozenset({"s", "n", "t", "d", "r", "e", "us", "nt", "ce"})


# ── Column classification ─────────────────────────────────────────────────────

_NUMERIC_COL_KEYWORDS = frozenset({
    "date", "account", "utr", "s no", " id", "transaction id", "ifsc",
})


def _is_numeric_col(col: str) -> bool:
    """
    Return True if *col* holds structured numeric data (dates, IDs, account
    numbers). Remark / reference fields are excluded so prose numbers are
    never accidentally collapsed.

    >>> _is_numeric_col("Transac tion Date & Time")
    True
    >>> _is_numeric_col("Transaction Date & Time")
    True
    >>> _is_numeric_col("Referen ce No / Remark s")
    False
    >>> _is_numeric_col("Reference No/Remarks")
    False
    """
    c = col.lower()
    if "remark" in c:
        return False
    return any(k in c for k in _NUMERIC_COL_KEYWORDS)


# ── Id-column resolution ──────────────────────────────────────────────────────

_ID_COL_SHAPES = frozenset({"sno", "srno", "serialno", "slno", "sirno"})


def _shape(name: str) -> str:
    """Reduce a column name to letters+digits only, lowercased."""
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def resolve_id_column(columns, preferred: str = None):
    """
    Return the name of the serial-number column as it actually appears in
    *columns*, or None when the table has no such column.

    Matching is by shape, so ``"S No"``, ``"S No."`` and ``"S. No."`` all
    resolve, which is what makes this safe across tables whose headers
    disagree about the trailing dot.

    >>> resolve_id_column(["S No", "Bank/FIs"])
    'S No'
    >>> resolve_id_column(["S No.", "Amount"])
    'S No.'
    >>> resolve_id_column(["Bank", "Amount"]) is None
    True
    """
    columns = list(columns)
    if preferred and preferred in columns:
        return preferred
    for col in columns:
        if _shape(col) in _ID_COL_SHAPES:
            return col
    return None


# ── Split-number repair ───────────────────────────────────────────────────────

_SPLIT_NUM_RE = re.compile(r"(\d{2,}) (\d{1,4})(?![\d:\/\-])")


def _fix_split_numbers(text: str) -> str:
    """
    Collapse digit-sequences broken by a line-wrap artefact.

    >>> _fix_split_numbers("202 6")
    '2026'
    >>> _fix_split_numbers("11/08/2026")   # already correct - untouched
    '11/08/2026'
    >>> _fix_split_numbers("2026 11:40:01")  # time separator guards it
    '2026 11:40:01'
    >>> _fix_split_numbers("11/08/202 6 10:11:AM")
    '11/08/2026 10:11:AM'
    >>> _fix_split_numbers("985045863 457")
    '985045863457'
    """
    prev = None
    out = text
    while prev != out:
        prev = out
        out = _SPLIT_NUM_RE.sub(lambda m: m.group(1) + m.group(2), out)
    return out


# ── Split-word repair ─────────────────────────────────────────────────────────

_LETTER_RUN = re.compile(r"[A-Za-z]+")


def _is_word(fragment: str) -> bool:
    f = fragment.lower()
    if f in _NEVER_STANDALONE:
        return False
    return f in DOMAIN_VOCAB


def repair_split_words(text: str, vocab=None) -> str:
    """
    Rejoin words broken across a cell wrap.

    Letter-runs separated by exactly one space are considered as join
    candidates, in groups of three first then two. A group is joined only
    when the concatenation is in *vocab* and at least one fragment is not
    itself a standalone word. Casing of the fragments is preserved.

    >>> repair_split_words("Transac tion Date & Time")
    'Transaction Date & Time'
    >>> repair_split_words("Dispute d Amount")
    'Disputed Amount'
    >>> repair_split_words("Stateme n t Ending Balance =2274.77")
    'Statement Ending Balance =2274.77'
    >>> repair_split_words("Fino Payment s Bank")
    'Fino Payments Bank'
    >>> repair_split_words("Account No.")          # not a split - untouched
    'Account No.'
    >>> repair_split_words("Date of Action")       # not a split - untouched
    'Date of Action'
    >>> repair_split_words("Bank of India")        # not a split - untouched
    'Bank of India'
    """
    if not text:
        return text
    vocab = DOMAIN_VOCAB if vocab is None else vocab

    changed = True
    while changed:
        changed = False
        runs = [(m.start(), m.end()) for m in _LETTER_RUN.finditer(text)]
        for size in (3, 2):
            for i in range(len(runs) - size + 1):
                group = runs[i:i + size]
                if any(text[a[1]:b[0]] != " " for a, b in zip(group, group[1:])):
                    continue
                parts = [text[s:e] for s, e in group]
                cand = "".join(parts)
                if cand.lower() not in vocab:
                    continue
                if all(_is_word(p) for p in parts):
                    continue
                text = text[:group[0][0]] + cand + text[group[-1][1]:]
                changed = True
                break
            if changed:
                break
    return text


# ── Column-name normalization ─────────────────────────────────────────────────

def normalize_column_name(col: str, normalize_slashes: bool = True) -> str:
    """
    Repair a header cell: rejoin split words, tidy slash spacing, collapse
    runs of whitespace.

    >>> normalize_column_name("Transac tion ID / UTR Number")
    'Transaction ID/UTR Number'
    >>> normalize_column_name("Referen ce No / Remark s")
    'Reference No/Remarks'
    >>> normalize_column_name("Bank /FIs")
    'Bank/FIs'
    >>> normalize_column_name("Account No./ Wallet ID")
    'Account No./Wallet ID'
    >>> normalize_column_name("")
    ''
    """
    if not col:
        return col
    out = repair_split_words(col)
    if normalize_slashes:
        out = re.sub(r"\s*/\s*", "/", out)
    out = re.sub(r"\s+", " ", out).strip()
    return out


def normalize_columns(columns, normalize_slashes: bool = True):
    """
    Normalize a full header list, guaranteeing the output has the same
    length and no duplicate names. If a repair would collide with a name
    already taken, the original spelling is kept for that column.

    Returns (new_columns, rename_map).
    """
    new_columns = []
    taken = set()
    rename = {}
    for col in columns:
        candidate = normalize_column_name(col, normalize_slashes)
        if candidate in taken and candidate != col:
            candidate = col          # collision - keep the original
        if candidate in taken:
            candidate = col
        new_columns.append(candidate)
        taken.add(candidate)
        rename[col] = candidate
    return new_columns, rename


# ── Value normalization ───────────────────────────────────────────────────────

def normalize_value(value: str, column: str) -> str:
    """
    Repair a single cell. Split-number collapsing is applied only to
    structured-numeric columns; split-word repair is applied everywhere.

    >>> normalize_value("11/08/202 6 10:11:AM", "Transaction Date & Time")
    '11/08/2026 10:11:AM'
    >>> normalize_value("985045863 457", "Transaction ID/UTR Number")
    '985045863457'
    >>> normalize_value("Fino Payment s", "Action Taken By")
    'Fino Payments'
    >>> normalize_value("Lien Marked:2356.8, Amount on Hold : 2356.8", "Reference No/Remarks")
    'Lien Marked:2356.8, Amount on Hold : 2356.8'
    """
    if not value:
        return value
    out = value
    if _is_numeric_col(column):
        out = _fix_split_numbers(out)
    out = repair_split_words(out)
    out = re.sub(r"[ \t]{2,}", " ", out).strip()
    return out


# ── Phantom-row merging ───────────────────────────────────────────────────────

def _merge_field(prev_val: str, ghost_val: str) -> str:
    """
    Append *ghost_val* onto *prev_val* using the mid-word / word-boundary
    heuristic.

    >>> _merge_field("Upi Tranatio", "n")
    'Upi Tranation'
    >>> _merge_field("Stateme nt Ending", "Balance =276.50")
    'Stateme nt Ending Balance =276.50'
    >>> _merge_field("", "some value")
    'some value'
    """
    prev_val = (prev_val or "").strip()
    ghost_val = (ghost_val or "").strip()
    if not prev_val:
        return ghost_val
    if prev_val[-1].islower() and ghost_val[0].islower():
        return prev_val + ghost_val
    return prev_val + " " + ghost_val


def merge_phantom_rows(rows: list, id_column: str = None) -> list:
    """
    Remove phantom rows produced by pdfplumber's cell-wrap mis-detection and
    fold their content back into the real row they belong to.

    *id_column* now defaults to None, meaning "resolve it from the row keys"
    via ``resolve_id_column``. Passing it explicitly still works.

    A phantom row is any row (except the very first) whose *id_column* value
    is blank. Phantom rows always appear immediately after the real row they
    were split from, so the merge target is always ``result[-1]``.

    Rows that absorbed a phantom are recorded in
    ``merge_phantom_rows.last_merged_ids``.

    >>> rows = [
    ...     {"S No.": "9",  "Referen ce No / Remark s": "Upi Tranatio"},
    ...     {"S No.": "",   "Referen ce No / Remark s": "n"},           # phantom
    ... ]
    >>> out = merge_phantom_rows(rows)
    >>> len(out)
    1
    >>> out[0]["Referen ce No / Remark s"]
    'Upi Tranation'
    >>> merge_phantom_rows.last_merged_ids
    {'9'}
    """
    if not rows:
        merge_phantom_rows.last_merged_ids = set()
        return []

    if id_column is None:
        id_column = resolve_id_column(rows[0].keys())
    if id_column is None:
        merge_phantom_rows.last_merged_ids = set()
        return [dict(r) for r in rows]

    result = []
    merged_ids = set()

    for row in rows:
        id_val = (row.get(id_column) or "").strip()

        if id_val:
            result.append(dict(row))
        else:
            if not result:
                warnings.warn(
                    f"merge_phantom_rows: first extracted row has a blank "
                    f"'{id_column}' - keeping it without merging. "
                    "Check the source PDF layout.",
                    UserWarning,
                    stacklevel=2,
                )
                result.append(dict(row))
                continue

            prev = result[-1]
            for field, phantom_val in row.items():
                phantom_val = (phantom_val or "").strip()
                if not phantom_val:
                    continue
                prev[field] = _merge_field(prev.get(field, ""), phantom_val)

            merged_ids.add(prev.get(id_column))

    merge_phantom_rows.last_merged_ids = merged_ids
    return result


# ── Sequence validation ───────────────────────────────────────────────────────

def validate_sno_sequence(rows: list, id_column: str = None) -> dict:
    """
    Inspect the serial-number column and decide whether anomalies are
    *certain* wrap-artifacts (safe to fix in place) or *ambiguous*
    extraction errors that should be escalated.

    ``recommended_action`` is one of:
      * ``"no_action_needed"``
      * ``"merge_blank_rows_into_previous"``
      * ``"escalate_to_camelot"``

    *id_column* defaults to None, meaning resolve it from the row keys.
    """
    if id_column is None and rows:
        id_column = resolve_id_column(rows[0].keys())

    if id_column is None:
        return {
            "id_column":          None,
            "is_clean_sequence":  True,
            "expected_count":     len(rows),
            "actual_row_count":   len(rows),
            "blank_row_indices":  [],
            "duplicate_values":   [],
            "confidence":         "certain",
            "recommended_action": "no_action_needed",
        }

    blank_indices = []
    raw_values = []

    for idx, row in enumerate(rows):
        val = (row.get(id_column) or "").strip()
        if val:
            raw_values.append(val)
        else:
            blank_indices.append(idx)

    int_values = []
    non_integer_found = False
    for v in raw_values:
        try:
            int_values.append(int(v))
        except ValueError:
            non_integer_found = True

    n = len(raw_values)
    seen = {}
    for v in int_values:
        seen[v] = seen.get(v, 0) + 1
    duplicates = [v for v, cnt in seen.items() if cnt > 1]

    expected_set = set(range(1, n + 1))
    actual_set = set(int_values)
    is_complete = (actual_set == expected_set) and not non_integer_found

    if n == 0 and len(rows) > 0:
        confidence, recommended_action = "ambiguous", "escalate_to_camelot"
    elif duplicates:
        confidence, recommended_action = "ambiguous", "escalate_to_camelot"
    elif not is_complete:
        confidence, recommended_action = "ambiguous", "escalate_to_camelot"
    elif blank_indices:
        confidence, recommended_action = "certain", "merge_blank_rows_into_previous"
    else:
        confidence, recommended_action = "certain", "no_action_needed"

    return {
        "id_column":          id_column,
        "is_clean_sequence":  is_complete,
        "expected_count":     n,
        "actual_row_count":   len(rows),
        "blank_row_indices":  blank_indices,
        "duplicate_values":   duplicates,
        "confidence":         confidence,
        "recommended_action": recommended_action,
    }


# ── Merged-text integrity check ───────────────────────────────────────────────

_EXPECTED_VOCAB = frozenset({
    "transaction", "payment", "transfer", "bank", "success",
    "statement", "ending", "balance", "amount", "account",
})


def validate_merged_text(rows: list, merged_ids: set, id_column: str = None) -> dict:
    """
    Check text fields of rows that received a phantom-row merge for signs of
    silent character loss at the pdfplumber text-layer level.

    A token is only reported when the *de-spaced* field still does not
    contain the expected word. That guard is what stops ``"Stateme nt"``
    from being reported: de-spaced it contains ``"statement"``, so it is a
    spacing artifact, not character loss. ``"Tranatio n"`` -> ``"Tranation"``
    has genuinely lost characters and is still reported.
    """
    if id_column is None and rows:
        id_column = resolve_id_column(rows[0].keys())

    suspicious = []
    for row in rows:
        if id_column is None or row.get(id_column) not in merged_ids:
            continue
        for col, val in row.items():
            if not val:
                continue
            despaced = re.sub(r"\s+", "", val).lower()
            for word in re.findall(r"[A-Za-z]{5,}", val):
                close = difflib.get_close_matches(
                    word.lower(), _EXPECTED_VOCAB, n=1, cutoff=0.75
                )
                if not close or close[0] == word.lower():
                    continue
                if close[0] in despaced:
                    continue          # spacing artifact, not character loss
                suspicious.append({
                    "row_id": row.get(id_column),
                    "column": col,
                    "found": word,
                    "expected_like": close[0],
                })

    return {
        "confidence": "ambiguous" if suspicious else "certain",
        "recommended_action": "escalate_to_vlm" if suspicious else "no_action_needed",
        "suspicious_tokens": suspicious,
    }


# ── Table-level orchestration ─────────────────────────────────────────────────

_ESCALATION_RANK = {
    "no_action_needed": 0,
    "merge_blank_rows_into_previous": 0,
    "escalate_to_vlm": 1,
    "escalate_to_camelot": 2,
}


def normalize_table(payload: dict, normalize_slashes: bool = True) -> dict:
    """
    Normalize one extractor payload.

    Accepts either shape the extractor emits:
      * ``{"table", "columns", "rows"}``  - the seven row tables
      * ``{"table", "data"}``             - complaint_meta (flat key/value)

    Returns a new payload of the same shape, plus a ``"_normalization"``
    report. No field is ever added to or removed from a row.
    """
    payload = copy.deepcopy(payload)
    table_name = payload.get("table", "<unknown>")

    # complaint_meta: flat key/value dict, no rows, no phantom logic.
    if "data" in payload and "rows" not in payload:
        data = payload.get("data") or {}
        new_data = {}
        for key, val in data.items():
            new_key = normalize_column_name(key, normalize_slashes)
            if new_key in new_data:
                new_key = key
            new_data[new_key] = normalize_value(val, key)
        payload["data"] = new_data
        payload["_normalization"] = {
            "table": table_name,
            "kind": "key_value",
            "fields": len(new_data),
            "confidence": "certain",
            "recommended_action": "no_action_needed",
        }
        return payload

    columns = payload.get("columns", [])
    rows = payload.get("rows", [])

    id_column = resolve_id_column(columns)
    sequence = validate_sno_sequence(rows, id_column)

    # Only merge when the sequence check says the blanks are certain
    # artifacts. An ambiguous sequence is handed on untouched so the
    # caller can escalate rather than guess.
    if sequence["recommended_action"] == "merge_blank_rows_into_previous":
        rows = merge_phantom_rows(rows, id_column)
        merged_ids = merge_phantom_rows.last_merged_ids
    else:
        rows = [dict(r) for r in rows]
        merged_ids = set()

    text_check = validate_merged_text(rows, merged_ids, id_column)

    # Values first (using original column names for numeric classification),
    # then rename the keys.
    new_columns, rename = normalize_columns(columns, normalize_slashes)

    new_rows = []
    for row in rows:
        new_row = {}
        for col, val in row.items():
            new_row[rename.get(col, normalize_column_name(col, normalize_slashes))] = \
                normalize_value(val, col)
        new_rows.append(new_row)

    # text_check ran against the pre-rename keys; report the new spelling.
    for token in text_check["suspicious_tokens"]:
        token["column"] = rename.get(token["column"], token["column"])

    payload["columns"] = new_columns
    payload["rows"] = new_rows

    actions = [sequence["recommended_action"], text_check["recommended_action"]]
    overall = max(actions, key=lambda a: _ESCALATION_RANK.get(a, 0))

    payload["_normalization"] = {
        "table": table_name,
        "kind": "rows",
        "id_column": id_column,
        "rows_in": sequence["actual_row_count"],
        "rows_out": len(new_rows),
        "phantom_rows_merged": len(sequence["blank_row_indices"])
                               if merged_ids else 0,
        "merged_row_ids": sorted(merged_ids, key=lambda x: (len(str(x)), str(x))),
        "renamed_columns": {k: v for k, v in rename.items() if k != v},
        "sequence": sequence,
        "text_check": text_check,
        "confidence": "certain" if _ESCALATION_RANK.get(overall, 0) == 0
                      else "ambiguous",
        "recommended_action": overall,
    }
    return payload


def normalize_directory(input_dir, output_dir=None,
                        normalize_slashes: bool = True, progress=None) -> dict:
    """
    Normalize every ``*.json`` file in *input_dir* that looks like an
    extractor payload. Writes to *output_dir*, or in place when it is None.

    Returns ``{table_name: output_path}``.
    """
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
                print(f"  [normalize] SKIP (not valid JSON): {path.name}")
                continue

        if not isinstance(payload, dict) or "table" not in payload:
            print(f"  [normalize] SKIP (not an extractor payload): {path.name}")
            continue

        result = normalize_table(payload, normalize_slashes)
        out_path = output_dir / path.name
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)

        report = result["_normalization"]
        written[result["table"]] = out_path
        if progress:
            progress(f"Normalized {result['table'].replace('_', ' ')}.")
        flag = "" if report["recommended_action"] in (
            "no_action_needed", "merge_blank_rows_into_previous"
        ) else f"  <-- {report['recommended_action'].upper()}"
        if report["kind"] == "rows":
            print(
                f"  [normalize] {result['table']:<24} "
                f"{report['rows_in']:>3} -> {report['rows_out']:>3} rows, "
                f"{len(report['renamed_columns'])} column(s) renamed{flag}"
            )
        else:
            print(
                f"  [normalize] {result['table']:<24} "
                f"{report['fields']} field(s){flag}"
            )
    return written


# ── Tests ─────────────────────────────────────────────────────────────────────

def _run_tests() -> None:
    """Unit + doctest suite. Exits 0 on full pass, 1 on failure."""
    import doctest

    PASS = "\033[32mPASS\033[0m"
    FAIL = "\033[31mFAIL\033[0m"
    results = []

    def check(name, ok, detail=""):
        results.append((name, ok, detail))
        line = f"  [{PASS if ok else FAIL}] {name}"
        if not ok:
            line += f"\n         {detail}"
        print(line)

    COLS = [
        "S No.", "Bank /FIs", "Account No./IFSC Code",
        "Transac tion ID / UTR Number", "Transac tion Date & Time",
        "Transacti on Amount", "Dispute d Amount",
        "Referen ce No / Remark s", "Action Taken By", "Date of Action",
    ]

    def row(**kw):
        return {c: kw.get(c, "") for c in COLS}

    raw = [row(**{"S No.": str(i)}) for i in range(1, 9)]
    raw.append(row(**{
        "S No.": "9",
        "Bank /FIs": "HDFC Bank",
        "Referen ce No / Remark s":
            "UPI-INDIA SHELTER FINANC-indiashelter.payu@indus-INDB0MERCHA-"
            "198147987-Upi Tranatio",
        "Action Taken By": "HDFC Bank",
        "Transac tion Date & Time": "11/08/202",
    }))
    raw.append(row(**{                                    # Phantom A
        "Referen ce No / Remark s": "n",
        "Transac tion Date & Time": "6 12:00:PM",
    }))
    raw += [row(**{"S No.": str(i)}) for i in range(10, 22)]
    raw.append(row(**{
        "S No.": "22",
        "Referen ce No / Remark s": "Stateme nt Ending",
        "Action Taken By": "Fino Payment s",
    }))
    raw.append(row(**{                                    # Phantom B
        "Referen ce No / Remark s": "Balance =276.50",
        "Action Taken By": "Bank",
    }))
    raw += [row(**{"S No.": str(i)}) for i in range(23, 31)]
    assert len(raw) == 32

    print("\n" + "=" * 64)
    print("  doctests")
    print("=" * 64)
    dt = doctest.testmod(sys.modules[__name__], verbose=False)
    check(f"doctests ({dt.attempted} run)", dt.failed == 0,
          f"{dt.failed} doctest failure(s)")

    print("\n" + "=" * 64)
    print("  resolve_id_column")
    print("=" * 64)
    check("'S No' (no dot) resolves",
          resolve_id_column(["S No", "Bank/FIs"]) == "S No")
    check("'S No.' resolves",
          resolve_id_column(["S No.", "Amount"]) == "S No.")
    check("absent -> None",
          resolve_id_column(["Bank", "Amount"]) is None)

    print("\n" + "=" * 64)
    print("  repair_split_words - must not damage correct text")
    print("=" * 64)
    for src in ["Account No.", "Date of Action", "Bank of India",
                "Put on hold Amount", "Amount Pending", "Card Details",
                "No. of Transaction Pending", "Money Transfer Through UPI.",
                "Action Taken By", "India Post Payments Bank"]:
        check(f"untouched: {src!r}", repair_split_words(src) == src,
              f"became {repair_split_words(src)!r}")

    print("\n" + "=" * 64)
    print("  repair_split_words - must repair")
    print("=" * 64)
    for src, want in [
        ("Transac tion Date & Time", "Transaction Date & Time"),
        ("Transacti on Amount", "Transaction Amount"),
        ("Dispute d Amount", "Disputed Amount"),
        ("Referen ce No / Remark s", "Reference No / Remarks"),
        ("Fino Payment s Bank", "Fino Payments Bank"),
        ("Stateme n t Ending Balance =2274.77",
         "Statement Ending Balance =2274.77"),
        ("Lien_Sta t us: No Amount", "Lien_Status: No Amount"),
        ("Root_UT R: 047699592609", "Root_UTR: 047699592609"),
    ]:
        got = repair_split_words(src)
        check(f"{src!r} -> {want!r}", got == want, f"got {got!r}")

    print("\n" + "=" * 64)
    print("  validate_sno_sequence")
    print("=" * 64)
    v = validate_sno_sequence(raw)
    check("is_clean_sequence = True", v["is_clean_sequence"] is True, str(v))
    check("expected_count = 30", v["expected_count"] == 30,
          f"got {v['expected_count']}")
    check("actual_row_count = 32", v["actual_row_count"] == 32)
    check("blank_row_indices has 2", len(v["blank_row_indices"]) == 2)
    check("action = merge_blank_rows...",
          v["recommended_action"] == "merge_blank_rows_into_previous",
          f"got {v['recommended_action']}")

    dup = [row(**{"S No.": str(i)}) for i in range(1, 6)]
    dup.append(row(**{"S No.": "5"}))
    dup += [row(**{"S No.": str(i)}) for i in range(6, 11)]
    v2 = validate_sno_sequence(dup)
    check("duplicates -> escalate_to_camelot",
          v2["recommended_action"] == "escalate_to_camelot")
    check("duplicate_values = [5]", v2["duplicate_values"] == [5])

    gap = [row(**{"S No.": str(i)}) for i in [1, 2, 4, 5, 6]]
    v3 = validate_sno_sequence(gap)
    check("gap -> escalate_to_camelot",
          v3["recommended_action"] == "escalate_to_camelot")

    v4 = validate_sno_sequence([row(**{"S No.": str(i)}) for i in range(1, 11)])
    check("clean -> no_action_needed",
          v4["recommended_action"] == "no_action_needed")

    print("\n" + "=" * 64)
    print("  merge_phantom_rows")
    print("=" * 64)
    out = merge_phantom_rows(raw)
    check("32 raw -> 30 output rows", len(out) == 30, f"got {len(out)}")
    check("no blank S No.",
          not [i for i, r in enumerate(out) if not r["S No."].strip()])
    check("S No. sequence 1-30",
          [r["S No."] for r in out] == [str(i) for i in range(1, 31)])

    row9 = next(r for r in out if r["S No."] == "9")
    check("phantom A mid-word merge",
          "Tranation" in row9["Referen ce No / Remark s"])
    row22 = next(r for r in out if r["S No."] == "22")
    check("phantom B multi-field merge",
          row22["Action Taken By"] == "Fino Payment s Bank",
          f"got {row22['Action Taken By']!r}")

    snapshot = copy.deepcopy(raw)
    merge_phantom_rows(raw)
    check("input not mutated", raw == snapshot)
    check("empty input", merge_phantom_rows([]) == [])

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        res = merge_phantom_rows([row(**{})])
    check("UserWarning on leading blank",
          any(issubclass(w.category, UserWarning) for w in caught))
    check("leading blank row kept", len(res) == 1)

    print("\n" + "=" * 64)
    print("  normalize_table - no field added or removed")
    print("=" * 64)
    payload = {"table": "lien_transactions", "columns": COLS, "rows": raw}
    norm = normalize_table(payload)
    check("row count 32 -> 30", len(norm["rows"]) == 30)
    check("columns length preserved",
          len(norm["columns"]) == len(COLS), f"got {len(norm['columns'])}")
    check("every row has same field count",
          all(len(r) == len(COLS) for r in norm["rows"]))
    check("column names repaired",
          "Transaction Date & Time" in norm["columns"],
          f"got {norm['columns']}")
    check("no duplicate column names",
          len(set(norm["columns"])) == len(norm["columns"]))

    print("\n" + "=" * 64)
    print("  normalize_table - complaint_transactions uses 'S No' (no dot)")
    print("=" * 64)
    ct_cols = ["S No", "Bank/FIs", "Complaint Date"]
    ct_rows = [{"S No": str(i), "Bank/FIs": "Indian Bank",
                "Complaint Date": "11/08/202 6 13:15:PM"} for i in range(1, 7)]
    ct = normalize_table({"table": "complaint_transactions",
                          "columns": ct_cols, "rows": ct_rows})
    check("all 6 rows survive (regression: used to collapse to 1)",
          len(ct["rows"]) == 6, f"got {len(ct['rows'])}")
    check("id_column resolved as 'S No'",
          ct["_normalization"]["id_column"] == "S No")
    check("split date repaired",
          ct["rows"][0]["Complaint Date"] == "11/08/2026 13:15:PM",
          f"got {ct['rows'][0]['Complaint Date']!r}")

    print("\n" + "=" * 64)
    print("  normalize_table - complaint_meta (key/value shape)")
    print("=" * 64)
    meta = normalize_table({
        "table": "complaint_meta",
        "data": {"Complaint Accepted Date": "12/08/2026 11:06:35 AM",
                 "Current Status": "Under Process"},
    })
    check("data preserved", len(meta["data"]) == 2, str(meta["data"]))
    check("no rows key added", "rows" not in meta)

    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print(f"\n{'=' * 64}")
    print(f"  {passed}/{total} checks passed"
          + ("  \u2713" if passed == total else "  \u2717  - see FAIL lines"))
    print("=" * 64)
    sys.exit(0 if passed == total else 1)


def main():
    args = [a for a in sys.argv[1:]]
    if args and args[0] in ("--self-test", "--test"):
        _run_tests()
        return
    if not args:
        print("Usage: python normalization.py <input_dir> [output_dir]")
        print("       python normalization.py --self-test")
        sys.exit(1)

    input_dir = args[0]
    output_dir = args[1] if len(args) >= 2 else None
    print(f"Normalizing extractor output in: {input_dir}")
    written = normalize_directory(input_dir, output_dir)
    print(f"\nNormalized {len(written)} table(s):")
    for name, path in written.items():
        print(f"  [{name}]  ->  {path}")


if __name__ == "__main__":
    main()
