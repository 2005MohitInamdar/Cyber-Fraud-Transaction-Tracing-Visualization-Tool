"""
services/requisition/collect.py
────────────────────────────────
Collect and group incomplete graph items into per-bank requisition buckets.

Sources
-------
- ``graph_payload["incomplete"]``  – incomplete transaction nodes
- ``graph_payload["orphanHolds"]`` – holds with no matched trail

Bank identification
-------------------
- Node       → ``item["bank"]``  (may be empty → __unknown__)
- Orphan hold → ``item["actionTakenBy"]``  (may be empty → __unknown__)

Reason codes
------------
Actionable (included in email asks):
    END_NO_STATUS, UNACCOUNTED_AMOUNT, NO_INCOMING_LINK, HOLD_WITHOUT_TRAIL

Internal (excluded, item dropped to `skipped`):
    VICTIM_UNTRACED, AMOUNT_ESTIMATED, LOW_CONFIDENCE_LINK

Items whose only codes are internal go to `skipped` with reason
"Only internal codes (VICTIM_UNTRACED / AMOUNT_ESTIMATED /
LOW_CONFIDENCE_LINK) — not actionable for banks."
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from .bank_names import normalize_bank_name

# ── Reason code constants ─────────────────────────────────────────────────────

ACTIONABLE_CODES: frozenset[str] = frozenset({
    "END_NO_STATUS",
    "UNACCOUNTED_AMOUNT",
    "NO_INCOMING_LINK",
    "HOLD_WITHOUT_TRAIL",
})

INTERNAL_CODES: frozenset[str] = frozenset({
    "VICTIM_UNTRACED",
    "AMOUNT_ESTIMATED",
    "LOW_CONFIDENCE_LINK",
})

# Template strings for each actionable ask.  {unaccounted} is formatted inline.
_ASKS: dict[str, str] = {
    "END_NO_STATUS": (
        "Please state where the credited amount was transferred or withdrawn, "
        "giving the beneficiary account number, IFSC, UTR/reference number, "
        "date and time of each onward transaction, or confirm that the amount "
        "is still available in the account."
    ),
    "UNACCOUNTED_AMOUNT": (
        "Please account for the remaining amount of Rs. {unaccounted}: "
        "onward transfers, withdrawals, or the amount currently under lien."
    ),
    "NO_INCOMING_LINK": (
        "Please provide the remitter/source details "
        "(account number, bank, UTR) of this credit."
    ),
    "HOLD_WITHOUT_TRAIL": (
        "Please confirm the account and the transaction against which this "
        "lien/hold was placed."
    ),
}

# ── Control-character stripper ────────────────────────────────────────────────

_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")


def _clean(value: Any) -> str:
    """Return a safe single-line string; never None / nan / etc."""
    if value is None:
        return ""
    return _CTRL_RE.sub(" ", str(value)).strip()


def _format_money(value: Any) -> str:
    """Format a numeric amount as '1,234.50'. Returns '' if missing."""
    if value is None:
        return ""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return _clean(value)
    # en-IN style grouping (2,00,000.00) via Python's manual approach
    # Simple thousands-only grouping is fine for bank comms.
    return f"{f:,.2f}"


# ── Item builder ─────────────────────────────────────────────────────────────

def _build_item(raw: dict[str, Any]) -> dict[str, Any]:
    """
    Construct the canonical item dict from a graph incomplete/orphanHold entry.
    All fields are strings to keep the JSON stable and safe for template use.
    """
    codes = [r["code"] for r in (raw.get("reasons") or [])]
    actionable = [c for c in codes if c in ACTIONABLE_CODES]

    unaccounted = raw.get("unaccountedAmount") or raw.get("amount")
    asks: list[str] = []
    for code in actionable:
        template = _ASKS[code]
        if code == "UNACCOUNTED_AMOUNT" and unaccounted:
            ask = template.format(unaccounted=_format_money(unaccounted))
        else:
            ask = template
        asks.append(ask)

    return {
        "refId":               _clean(raw.get("nodeId") or raw.get("holdId")),
        "layer":               raw.get("layer"),
        "accountNo":           _clean(raw.get("accountNo")),
        "utr":                 _clean(raw.get("utr")),
        "transactionDateTime": _clean(raw.get("transactionDateTime") or raw.get("date")),
        "transactionAmount":   _format_money(raw.get("txAmount")),
        "disputedAmount":      _format_money(raw.get("disputedAmount") or raw.get("amount")),
        "unaccountedAmount":   _format_money(unaccounted),
        "reasonCodes":         actionable,
        "asks":                asks,
    }


def _stable_sort_key(item: dict[str, Any]) -> tuple:
    """Sort key for stable, deterministic item ordering."""
    return (
        item.get("layer") or 0,
        item.get("accountNo") or "",
        item.get("utr") or "",
    )


def _compute_item_hash(items: list[dict[str, Any]]) -> str:
    """SHA-256 of the canonical JSON of sorted items (sorted keys + sorted list)."""
    canonical = json.dumps(
        sorted(items, key=lambda x: _stable_sort_key(x)),
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ── Public API ────────────────────────────────────────────────────────────────

def collect_requisition_items(
    graph_payload: dict[str, Any],
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """
    Group incomplete items from *graph_payload* into per-bank buckets.

    Parameters
    ----------
    graph_payload : dict
        The response from ``get_case_graph`` / ``build_graph_payload``.
        Must contain ``"incomplete"`` and ``"orphanHolds"`` lists.

    Returns
    -------
    (buckets, skipped)

    buckets : dict[bankKey, {bankName, items, itemHash}]
        bankKey = normalized_bank_name result (or ``"__unknown__"``).
        ``items`` are sorted by (layer, accountNo, utr) for stability.
        ``itemHash`` = sha256 of the canonical JSON of that bank's items.

    skipped : list[{refId, reason}]
        Items that were dropped because they had only internal reason codes
        or ended up with no asks.
    """
    buckets: dict[str, dict[str, Any]] = {}
    skipped: list[dict[str, Any]] = []

    sources: list[tuple[dict[str, Any], str]] = []

    for item in (graph_payload.get("incomplete") or []):
        bank = _clean(item.get("bank"))
        sources.append((item, bank))

    for item in (graph_payload.get("orphanHolds") or []):
        bank = _clean(item.get("actionTakenBy"))
        sources.append((item, bank))

    for raw, bank_raw in sources:
        codes = [r["code"] for r in (raw.get("reasons") or [])]
        actionable = [c for c in codes if c in ACTIONABLE_CODES]
        ref_id = _clean(raw.get("nodeId") or raw.get("holdId"))

        if not actionable:
            skipped.append({
                "refId":  ref_id,
                "bank":   bank_raw or "Unknown",
                "reason": (
                    "Only internal reason codes "
                    f"({', '.join(codes) if codes else 'none'}) "
                    "— not actionable for banks."
                ),
            })
            continue

        # Determine bank key
        if bank_raw:
            bank_key = normalize_bank_name(bank_raw)
            bank_display = bank_raw
        else:
            bank_key = "__unknown__"
            bank_display = "Bank not identified"

        item_data = _build_item(raw)

        if bank_key not in buckets:
            buckets[bank_key] = {
                "bankName": bank_display,
                "bankKey":  bank_key,
                "items":    [],
            }

        buckets[bank_key]["items"].append(item_data)

    # Sort items within each bucket and compute hash
    for bucket in buckets.values():
        bucket["items"].sort(key=_stable_sort_key)
        bucket["itemHash"] = _compute_item_hash(bucket["items"])

    return buckets, skipped
