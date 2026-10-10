"""
services/requisition/template.py
──────────────────────────────────
Build a plain-text requisition email from fixed wording.
NO LLM calls, NO model API, NO web requests.

Two modes (build_email's `table_in_body` flag)
----------------------------------------------
table_in_body=True   Intro + full text table + key + footer (all in the body).
table_in_body=False  Intro + short note + footer. The table and key are in an
                     attached PDF (see pdf_table.py), which reads far better on
                     phones than a wide plain-text table.

Rules
-----
- Refuse to build if ack_no is missing or blank.
- Strip control characters and newlines from every inserted value so no
  injected text can create extra headers or fake paragraphs.
- Money is formatted with comma grouping and two decimals.
- "Not available" is printed for account/UTR/date when the value is empty.
- Disputed / Unaccounted columns are dropped when empty for EVERY item.
- Officer details (name, rank, branch) come in via the `officer` dict.
- `legal_reference` is the REQUISITION_LEGAL_REFERENCE env variable (may be "").
"""

from __future__ import annotations

import os
import re
from typing import Any

# ── Control-character cleaner ─────────────────────────────────────────────────

_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]+")


def _safe(value: Any, fallback: str = "") -> str:
    """Return a clean single-line string, using *fallback* when empty."""
    if value is None:
        return fallback
    cleaned = _CTRL_RE.sub(" ", str(value)).strip()
    return cleaned if cleaned else fallback


def _money(value: Any) -> str:
    """Format a numeric string/float as '1,234.50'. Returns '' if empty."""
    if not value:
        return ""
    try:
        f = float(str(value).replace(",", ""))
        return f"{f:,.2f}"
    except (TypeError, ValueError):
        return _safe(value)


# ── Information legend (each ask is stated once) ──────────────────────────────
# Order here = order of letters. Codes match ACTIONABLE_CODES in collect.py.
# pdf_table.py imports _CODE_ORDER, _CODE_TEXT and _letters_for from here.

_LEGEND: list[tuple[str, str]] = [
    (
        "END_NO_STATUS",
        "Onward movement of the credited amount: where it was transferred or "
        "withdrawn, giving the beneficiary account number, IFSC, UTR/reference "
        "number, and date and time of each onward transaction, OR confirmation "
        "that the amount is still available in the account.",
    ),
    (
        "UNACCOUNTED_AMOUNT",
        "Details of the amount shown under 'Unaccounted': onward transfers, "
        "withdrawals, or the amount currently under lien.",
    ),
    (
        "NO_INCOMING_LINK",
        "Remitter/source details of this credit (account number, bank, UTR).",
    ),
    (
        "HOLD_WITHOUT_TRAIL",
        "The account and the transaction against which this lien/hold was placed.",
    ),
]
_CODE_ORDER = [code for code, _ in _LEGEND]
_CODE_TEXT = dict(_LEGEND)


# ── Template ──────────────────────────────────────────────────────────────────

_SUBJECT_TMPL = (
    "Request for transaction details - Ack No. {ack_no} - {bank_name}"
)

# Wording that differs between the two modes lives in these placeholders.
_BODY_INTRO = """\
To,
The Nodal Officer,
{bank_name}

Subject: Request for transaction details - NCRP Acknowledgement No. {ack_no}

Sir/Madam,

A complaint of online financial fraud has been registered on the National \
Cybercrime Reporting Portal under Acknowledgement No. {ack_no}. During the \
investigation, the accounts/transactions listed in {where} were found \
to involve your bank, and the information about the movement of the credited \
amount is incomplete.

You are requested to kindly provide the information indicated against each \
transaction (see the "Info required" column and the key {key_where}) at \
the earliest, in a reply to this email.\
"""

_BODY_FOOTER = """\

Please also continue to keep any lien/hold already placed on these accounts \
until further instructions.

Thanking you,

{inspector_name}
{inspector_rank}
{inspector_branch}
Reply to: {reply_to}\
"""


# ── Table + legend builders ───────────────────────────────────────────────────

def _letters_for(item: dict[str, Any]) -> list[str]:
    """Return the sorted legend letters that apply to this item."""
    codes = set(item.get("reasonCodes") or [])
    return [
        chr(ord("A") + idx)
        for idx, code in enumerate(_CODE_ORDER)
        if code in codes
    ]


def _build_table(items: list[dict[str, Any]]) -> str:
    """Render all items as one pipe-separated, width-aligned text table."""
    show_bank        = any(i.get("bankName") for i in items)
    show_disputed    = any(_money(i.get("disputedAmount"))    for i in items)
    show_unaccounted = any(_money(i.get("unaccountedAmount")) for i in items)

    # headers and rows are built with the same conditions, in the same order
    headers = ["No."]
    if show_bank:
        headers.append("Bank")
    headers += ["Account No.", "UTR / Ref. No.", "Date and time", "Amount (Rs.)"]
    if show_disputed:
        headers.append("Disputed (Rs.)")
    if show_unaccounted:
        headers.append("Unaccounted (Rs.)")
    headers.append("Info required")

    rows: list[list[str]] = []
    for n, item in enumerate(items, start=1):
        row = [str(n)]
        if show_bank:
            row.append(_safe(item.get("bankName"), "-"))
        row += [
            _safe(item.get("accountNo"), "Not available"),
            _safe(item.get("utr"), "Not available"),
            _safe(item.get("transactionDateTime"), "Not available"),
            _money(item.get("transactionAmount")) or "-",
        ]
        if show_disputed:
            row.append(_money(item.get("disputedAmount")) or "-")
        if show_unaccounted:
            row.append(_money(item.get("unaccountedAmount")) or "-")
        row.append(", ".join(_letters_for(item)) or "-")
        rows.append(row)

    widths = [
        max(len(headers[c]), *(len(r[c]) for r in rows))
        for c in range(len(headers))
    ]

    def fmt(cells: list[str]) -> str:
        return " | ".join(cell.ljust(widths[i]) for i, cell in enumerate(cells)).rstrip()

    sep = "-+-".join("-" * w for w in widths)
    return "\n".join([fmt(headers), sep, *(fmt(r) for r in rows)])


def _build_legend(items: list[dict[str, Any]]) -> str:
    """List each required piece of information once, only for codes in use."""
    used: set[str] = set()
    for item in items:
        used.update(item.get("reasonCodes") or [])

    lines = ["Information required (key to the table above):", ""]
    for idx, code in enumerate(_CODE_ORDER):
        if code in used:
            letter = chr(ord("A") + idx)
            lines.append(f"{letter}. {_CODE_TEXT[code]}")
            lines.append("")
    return "\n".join(lines).rstrip()


# ── Public API ────────────────────────────────────────────────────────────────

def build_email(
    bank_name: str,
    items: list[dict[str, Any]],
    ack_no: str,
    officer: dict[str, str],
    reply_to: str,
    legal_reference: str = "",
    table_in_body: bool = True,
) -> dict[str, str]:
    """
    Build the requisition email.

    Parameters
    ----------
    bank_name       : Display name of the bank (or "All concerned banks").
    items           : List of item dicts from collect.py.
    ack_no          : NCRP acknowledgement number (required, raises if blank).
    officer         : {inspectorName, inspectorRank, inspectorBranch}.
    reply_to        : Validated reply-to email address.
    legal_reference : Value of REQUISITION_LEGAL_REFERENCE env var (may be "").
    table_in_body   : True  -> text table + key inside the email body.
                      False -> short note only; table + key go in an attached PDF.

    Returns
    -------
    {"subject": str, "body_text": str}

    Raises
    ------
    ValueError – if ack_no is blank.
    """
    ack_no = _safe(ack_no)
    if not ack_no:
        raise ValueError("ack_no is required to build a requisition email.")

    bank_name = _safe(bank_name) or "Bank not identified"
    reply_to  = _safe(reply_to)

    inspector_name   = _safe(officer.get("inspectorName"))
    inspector_rank   = _safe(officer.get("inspectorRank"))
    inspector_branch = _safe(officer.get("inspectorBranch"))

    subject = _SUBJECT_TMPL.format(ack_no=ack_no, bank_name=bank_name)

    parts: list[str] = []

    # Optional legal reference line
    ref = _safe(legal_reference or os.environ.get("REQUISITION_LEGAL_REFERENCE", ""))
    if ref:
        parts.append(ref)
        parts.append("")

    # Intro (wording depends on where the table lives)
    if table_in_body:
        where, key_where = "the table below", "below the table"
    else:
        where, key_where = "the attached PDF", "in the attached PDF"
    parts.append(_BODY_INTRO.format(
        bank_name=bank_name, ack_no=ack_no, where=where, key_where=key_where,
    ))
    parts.append("")

    # Table + key in the body, or a short pointer to the PDF
    if items:
        if table_in_body:
            parts.append(_build_table(items))
            parts.append("")
            parts.append(_build_legend(items))
        else:
            parts.append(f"Number of transactions: {len(items)}")
            parts.append("Attachment: missing-data PDF (table and key inside).")

    # Footer
    parts.append(
        _BODY_FOOTER.format(
            inspector_name=inspector_name,
            inspector_rank=inspector_rank,
            inspector_branch=inspector_branch,
            reply_to=reply_to,
        )
    )

    return {"subject": subject, "body_text": "\n".join(parts)}