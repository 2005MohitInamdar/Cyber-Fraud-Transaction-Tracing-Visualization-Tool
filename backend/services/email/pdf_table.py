"""Render the missing-data table as a landscape A4 PDF (bytes)."""
from __future__ import annotations

from io import BytesIO
from typing import Any
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from .template import _CODE_ORDER, _CODE_TEXT, _letters_for, _money, _safe

_ss = getSampleStyleSheet()
_CELL = ParagraphStyle("cell", parent=_ss["Normal"], fontSize=8, leading=10)
_HEAD = ParagraphStyle("head", parent=_CELL, textColor=colors.white, fontName="Helvetica-Bold")


def _p(text: str, style=_CELL) -> Paragraph:
    return Paragraph(escape(text), style)   # escape: bank names can contain & or <


def build_requisition_pdf(items: list[dict[str, Any]], ack_no: str) -> bytes:
    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=landscape(A4),
        leftMargin=10 * mm, rightMargin=10 * mm, topMargin=12 * mm, bottomMargin=12 * mm,
        title=f"Missing transaction data - Ack No. {ack_no}",
    )

    headers = ["No.", "Bank", "Account No.", "UTR / Ref. No.", "Date and time",
               "Amount (Rs.)", "Disputed (Rs.)", "Unaccounted (Rs.)", "Info required"]
    widths = [10, 55, 38, 32, 30, 25, 25, 25, 22]          # mm, total 262 (page body = 277)

    data = [[_p(h, _HEAD) for h in headers]]
    for n, it in enumerate(items, start=1):
        data.append([_p(c) for c in [
            str(n),
            _safe(it.get("bankName"), "-"),
            _safe(it.get("accountNo"), "Not available"),
            _safe(it.get("utr"), "Not available"),
            _safe(it.get("transactionDateTime"), "Not available"),
            _money(it.get("transactionAmount")) or "-",
            _money(it.get("disputedAmount")) or "-",
            _money(it.get("unaccountedAmount")) or "-",
            ", ".join(_letters_for(it)) or "-",
        ]])

    table = Table(data, colWidths=[w * mm for w in widths], repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f2937")),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#9ca3af")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f3f4f6")]),
    ]))

    used = {c for it in items for c in (it.get("reasonCodes") or [])}
    story = [
        Paragraph(f"Request for transaction details - NCRP Ack No. {escape(ack_no)}",
                  _ss["Heading3"]),
        Spacer(1, 4 * mm),
        table,
        Spacer(1, 6 * mm),
        Paragraph("Information required (key to the table):", _ss["Heading4"]),
    ]
    for idx, code in enumerate(_CODE_ORDER):
        if code in used:
            story.append(Paragraph(f"<b>{chr(65 + idx)}.</b> {escape(_CODE_TEXT[code])}", _CELL))
            story.append(Spacer(1, 2 * mm))

    doc.build(story)
    return buf.getvalue()