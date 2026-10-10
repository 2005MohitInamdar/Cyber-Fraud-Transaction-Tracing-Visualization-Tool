"""Preview + send requisition emails using getMissingData → collect → template → mailer."""
from __future__ import annotations
import json
import os
import re
from datetime import datetime, timezone
from typing import Any, Optional
import hashlib
from .pdf_table import build_requisition_pdf
from pydantic import BaseModel, field_validator

from db.connection import get_connection
# from .bank_names import normalize_bank_name
from .collect import collect_requisition_items
from .getMissingData import get_missing_data
from .mailer import send_email
from .template import build_email

# MAX_BATCH = int(os.environ.get("REQUISITION_MAX_BATCH", "25"))

_EMAIL_RE = re.compile(
    r"^[a-zA-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?"
    r"(?:\.[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)*\.[a-zA-Z]{2,}$"
)


def _valid_email(addr: str) -> str:
    addr = addr.strip().lower()
    if re.search(r"[\s,<>\r\n]", addr) or not _EMAIL_RE.match(addr):
        raise ValueError(f"Invalid email address: {addr!r}")
    return addr


# ── Request models ────────────────────────────────────────────────────────────
class PreviewRequest(BaseModel):
    replyTo: str
    ackNo: Optional[str] = None          # only needed if cases.ack_no is empty

    @field_validator("replyTo")
    @classmethod
    def _rt(cls, v: str) -> str:
        return _valid_email(v)


class SinglePreviewRequest(BaseModel):
    replyTo: str
    ackNo: Optional[str] = None

    @field_validator("replyTo")
    @classmethod
    def _rt(cls, v: str) -> str:
        return _valid_email(v)


class SingleSendRequest(BaseModel):
    replyTo: str
    ackNo: Optional[str] = None
    to: list[str]
    itemHash: str

    @field_validator("replyTo")
    @classmethod
    def _rt(cls, v: str) -> str:
        return _valid_email(v)


# ── Helpers ───────────────────────────────────────────────────────────────────
def _test_recipient() -> str:
    return os.environ.get("REQUISITION_TEST_RECIPIENT", "").strip()


# def _is_real_bank(bank_key: str, bank_name: str) -> bool:
#     return bank_key != "__unknown__" and not bank_name.lower().startswith("others")


def _case_meta(user_id: str, upload_id: str, ack_override: str | None) -> tuple[str, dict]:
    with get_connection() as conn:
        with conn.cursor(dictionary=True) as cur:
            cur.execute(
                "SELECT inspector_name, inspector_rank, inspector_branch "
                "FROM fraud_case_uploads WHERE upload_id=%s AND supabase_user_id=%s LIMIT 1",
                (upload_id, user_id),
            )
            off = cur.fetchone()
            if off is None:
                raise PermissionError("You do not have access to this case.")
            cur.execute(
                "SELECT ack_no FROM cases WHERE upload_id=%s AND supabase_user_id=%s LIMIT 1",
                (upload_id, user_id),
            )
            case = cur.fetchone() or {}
    ack_no = (ack_override or case.get("ack_no") or "").strip()
    if not ack_no:
        raise ValueError("Acknowledgement number is missing; please provide ackNo.")
    officer = {
        "inspectorName":   off["inspector_name"],
        "inspectorRank":   off["inspector_rank"],
        "inspectorBranch": off["inspector_branch"],
    }
    return ack_no, officer


def _load_buckets(user_id: str, upload_id: str):
    payload = get_missing_data(user_id, upload_id)
    return collect_requisition_items(payload)


# ── Send ──────────────────────────────────────────────────────────────────────
def _insert_row(upload_id, user_id, ack_no, bank_name, normalized, to, delivered_to,
                subject, body, item_hash, test_mode, reply_to="") -> int:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO requisitions (upload_id, supabase_user_id, ack_no, bank_name, "
                "normalized_name, to_emails, delivered_to, reply_to, subject, body_text, "
                "item_hash, test_mode, status) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'sending')",
                (upload_id, user_id, ack_no, bank_name, normalized, json.dumps(to),
                 delivered_to, reply_to, subject, body, item_hash, int(test_mode)),
            )
            return cur.lastrowid


def _finish_row(row_id: int, ok: bool, message_id: str | None = None, error: str | None = None):
    with get_connection() as conn:
        with conn.cursor() as cur:
            if ok:
                cur.execute("UPDATE requisitions SET status='sent', message_id=%s, "
                            "sent_at=%s WHERE id=%s",
                            (message_id, datetime.now(timezone.utc), row_id))
            else:
                cur.execute("UPDATE requisitions SET status='failed', error_message=%s "
                            "WHERE id=%s", ((error or "")[:500], row_id))


# ── History ───────────────────────────────────────────────────────────────────
def list_requisitions(user_id: str, upload_id: str) -> dict[str, Any]:
    _case_meta_owner_check(user_id, upload_id)
    with get_connection() as conn:
        with conn.cursor(dictionary=True) as cur:
            cur.execute(
                "SELECT bank_name, to_emails, delivered_to, status, sent_at, "
                "error_message, test_mode, created_at FROM requisitions "
                "WHERE upload_id=%s AND supabase_user_id=%s ORDER BY id DESC",
                (upload_id, user_id),
            )
            rows = cur.fetchall()
    return {"history": [{
        "bankName":    r["bank_name"],
        "to":          json.loads(r["to_emails"]) if isinstance(r["to_emails"], str) else r["to_emails"],
        "deliveredTo": r["delivered_to"],
        "status":      r["status"],
        "sentAt":      r["sent_at"].isoformat() if r["sent_at"] else None,
        "error":       r["error_message"],
        "testMode":    bool(r["test_mode"]),
    } for r in rows]}


def _case_meta_owner_check(user_id: str, upload_id: str) -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM fraud_case_uploads WHERE upload_id=%s "
                        "AND supabase_user_id=%s LIMIT 1", (upload_id, user_id))
            if cur.fetchone() is None:
                raise PermissionError("You do not have access to this case.")


def _all_items(user_id: str, upload_id: str):
    buckets, skipped = _load_buckets(user_id, upload_id)
    items = [{**it, "bankName": b["bankName"]} for b in buckets.values() for it in b["items"]]
    items.sort(key=lambda i: (i["bankName"], i.get("layer") or 0,
                              i.get("accountNo") or "", i.get("utr") or ""))
    digest = hashlib.sha256(
        json.dumps(items, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    return items, digest, skipped


def preview_single(user_id: str, upload_id: str, body: SinglePreviewRequest) -> dict[str, Any]:
    ack_no, officer = _case_meta(user_id, upload_id, body.ackNo)
    items, digest, _ = _all_items(user_id, upload_id)
    if not items:
        return {"ackNo": ack_no, "itemCount": 0, "itemHash": digest,
                "subject": "", "bodyText": "", "testMode": bool(_test_recipient())}
    # email = build_email("All concerned banks", items, ack_no, officer, body.replyTo)
    email = build_email("All concerned banks", items, ack_no, officer, body.replyTo,
                        table_in_body=False)
    return {"ackNo": ack_no, "itemCount": len(items), "itemHash": digest,
            "subject": email["subject"], "bodyText": email["body_text"],
            "testMode": bool(_test_recipient())}


def send_single(user_id: str, upload_id: str, body: SingleSendRequest) -> dict[str, Any]:
    ack_no, officer = _case_meta(user_id, upload_id, body.ackNo)
    items, digest, _ = _all_items(user_id, upload_id)      # fresh from DB
    if not items:
        raise ValueError("Nothing to send.")
    if digest != body.itemHash:
        raise ValueError("Case data changed since preview; please regenerate.")
    if not 1 <= len(body.to) <= 5:
        raise ValueError("Provide 1 to 5 'to' addresses.")
    to = list(dict.fromkeys(_valid_email(a) for a in body.to))

    # email = build_email("All concerned banks", items, ack_no, officer, body.replyTo)
    email = build_email("All concerned banks", items, ack_no, officer, body.replyTo,
                        table_in_body=False)
    pdf = build_requisition_pdf(items, ack_no)
    pdf_name = "missing-data-" + re.sub(r"[^A-Za-z0-9_-]", "", ack_no) + ".pdf"
    subject, text, recipients = email["subject"], email["body_text"], to
    test_to = _test_recipient()
    if test_to:
        recipients = [test_to]
        subject = "[TEST] " + subject
        text = "Intended recipients: " + ", ".join(to) + "\n\n" + text

    row_id = _insert_row(upload_id, user_id, ack_no, "ALL BANKS", "__all__", to,
                        test_to or None, email["subject"], email["body_text"],
                        digest, bool(test_to), reply_to=body.replyTo)
    try:
        # msg_id = send_email(recipients, subject, text, reply_to=body.replyTo)
        msg_id = send_email(recipients, subject, text, reply_to=body.replyTo,
                            attachments=[(pdf_name, pdf)])
        _finish_row(row_id, True, message_id=msg_id)
        return {"status": "sent", "deliveredTo": recipients}
    except Exception as exc:
        print(f"[REQUISITION SMTP ERROR] {exc}")
        _finish_row(row_id, False, error=str(exc))
        raise RuntimeError("Email delivery failed.")