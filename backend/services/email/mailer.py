import os
import smtplib
from email.message import EmailMessage
from email.utils import make_msgid

from dotenv import load_dotenv

load_dotenv()


def _env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Environment variable {name!r} is not set.")
    return value


def send_email(to: list[str], subject: str, body: str,
               reply_to: str | None = None,
               attachments: list[tuple[str, bytes]] | None = None) -> str:
    """Send a plain-text email. Returns the Message-ID."""
    for v in [subject, reply_to or "", *to]:
        if "\r" in v or "\n" in v:
            raise ValueError("Header injection attempt: CR/LF in header value.")

    host, user, pwd = _env("SMTP_HOST"), _env("SMTP_USERNAME"), _env("SMTP_PASSWORD")
    from_addr = _env("SMTP_FROM_ADDRESS")
    port = int(os.getenv("SMTP_PORT", "587"))   # env vars are strings

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = ", ".join(to)
    if reply_to:
        msg["Reply-To"] = reply_to
    msg["Message-ID"] = make_msgid()
    msg.set_content(body)
    for filename, data in (attachments or []):
        if "\r" in filename or "\n" in filename:
            raise ValueError("Bad attachment filename.")
        msg.add_attachment(data, maintype="application", subtype="pdf", filename=filename)

    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=30) as s:
            s.login(user, pwd)
            s.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=30) as s:
            s.ehlo()
            s.starttls()
            s.ehlo()
            s.login(user, pwd)
            s.send_message(msg)
    return msg["Message-ID"]