"""
Dry-run by default:   python -m services.email.send_requisitions <user_id> <upload_id>
Actually send:        python -m services.email.send_requisitions <user_id> <upload_id> --send

All mail goes to REPORT_RECIPIENT_EMAIL (test mode) until bank contacts are wired in.
"""
import argparse
import os
import time

from dotenv import load_dotenv

from db.connection import get_connection
from .getMissingData import get_missing_data
from .collect import collect_requisition_items
from .template import build_email
from .mailer import send_email

load_dotenv()


def get_case_meta(user_id: str, upload_id: str) -> tuple[str, dict]:
    """ack_no from `cases`, officer details from `fraud_case_uploads`."""
    with get_connection() as conn:
        with conn.cursor(dictionary=True) as cur:
            cur.execute(
                "SELECT ack_no FROM cases WHERE upload_id=%s AND supabase_user_id=%s LIMIT 1",
                (upload_id, user_id),
            )
            case = cur.fetchone() or {}
            cur.execute(
                "SELECT inspector_name, inspector_rank, inspector_branch "
                "FROM fraud_case_uploads WHERE upload_id=%s AND supabase_user_id=%s LIMIT 1",
                (upload_id, user_id),
            )
            off = cur.fetchone()
    if off is None:
        raise PermissionError("No access to this case.")
    officer = {
        "inspectorName":   off["inspector_name"],
        "inspectorRank":   off["inspector_rank"],
        "inspectorBranch": off["inspector_branch"],
    }
    return (case.get("ack_no") or ""), officer


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("user_id")
    ap.add_argument("upload_id")
    ap.add_argument("--ack", help="override ack no if cases.ack_no is empty")
    ap.add_argument("--send", action="store_true", help="really send (default: dry run)")
    args = ap.parse_args()

    test_to = os.getenv("REPORT_RECIPIENT_EMAIL", "").strip()
    reply_to = os.getenv("SMTP_FROM_ADDRESS", "").strip()
    if args.send and not test_to:
        raise SystemExit("Set REPORT_RECIPIENT_EMAIL in .env before using --send.")

    ack_no, officer = get_case_meta(args.user_id, args.upload_id)
    ack_no = args.ack or ack_no
    if not ack_no:
        raise SystemExit("ack_no is empty in `cases`; pass --ack <number>.")

    payload = get_missing_data(args.user_id, args.upload_id)
    buckets, skipped = collect_requisition_items(payload)
    print(f"{len(buckets)} bank buckets, {len(skipped)} skipped items")

    for bank_key, bucket in buckets.items():
        bank_name = bucket["bankName"]

        # "Others (UPI-...)" and unknown entries are not banks: nobody to email
        if bank_key == "__unknown__" or bank_name.lower().startswith("others"):
            print(f"SKIP (not a bank): {bank_name[:60]}")
            continue

        email = build_email(
            bank_name=bank_name, items=bucket["items"], ack_no=ack_no,
            officer=officer, reply_to=reply_to,
        )
        subject = "[TEST] " + email["subject"]
        body = f"Intended recipient: {bank_name}\n\n" + email["body_text"]

        if not args.send:
            print("=" * 80, f"\nSubject: {subject}\n\n{body}\n")
            continue

        msg_id = send_email([test_to], subject, body, reply_to=reply_to or None)
        print(f"SENT  {bank_name}  ({len(bucket['items'])} items)  {msg_id}")
        time.sleep(1)


if __name__ == "__main__":
    main()