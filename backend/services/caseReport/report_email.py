"""Generate concise case-report emails and send them via configured SMTP."""

import os
import smtplib
from email.message import EmailMessage
from numbers import Number
from typing import Any

import requests


_OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
_DEFAULT_OPENROUTER_MODEL = "openai/gpt-4o-mini"


class ReportGenerationError(RuntimeError):
    """The LLM could not generate a report body."""


class ReportDeliveryError(RuntimeError):
    """SMTP could not deliver the generated report."""


def _required_environment(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"{name} is not configured.")
    return value


def _display_value(value: Any) -> str:
    if value is None or value == "":
        return "—"
    return str(value)


def _amount_summaries(rows: list[dict[str, Any]]) -> list[str]:
    totals: dict[str, float] = {}
    for row in rows:
        for key, value in row.items():
            normalized_key = key.replace("_", "").lower()
            if "amount" not in normalized_key or isinstance(value, bool):
                continue
            if isinstance(value, Number):
                totals[key] = totals.get(key, 0) + float(value)
            elif isinstance(value, str):
                try:
                    totals[key] = totals.get(key, 0) + float(value.replace(",", ""))
                except ValueError:
                    pass
    return [f"{key}: ₹{total:,.2f}" for key, total in totals.items()]


def _case_summary(case_detail: dict[str, Any]) -> str:
    lead = case_detail.get("leadOfficer", {})
    upload = case_detail.get("upload", {})
    tables = case_detail.get("tables", {})

    lines = [
        f"Upload ID: {_display_value(case_detail.get('uploadId'))}",
        "Inspector: " + ", ".join(
            _display_value(lead.get(key))
            for key in ("inspectorName", "inspectorRank", "inspectorBranch")
        ),
        f"File: {_display_value(upload.get('fileName'))}",
        f"Upload status: {_display_value(upload.get('status'))}",
        "Extraction summary:",
    ]

    for table_name, table_data in tables.items():
        if isinstance(table_data, list):
            amounts = _amount_summaries(table_data)
            summary = f"{table_name}: {len(table_data)} record(s)"
            if amounts:
                summary += "; " + ", ".join(amounts)
            lines.append(f"- {summary}")
        elif isinstance(table_data, dict):
            populated_fields = sum(value not in (None, "") for value in table_data.values())
            lines.append(f"- {table_name}: {populated_fields} populated metadata field(s)")

    return "\n".join(lines)


def request_openrouter_completion(system_prompt: str, user_prompt: str) -> str:
    """Return one OpenRouter chat-completion message without exposing its key."""
    api_key = _required_environment("OPENROUTER_API_KEY")
    model = os.getenv("OPENROUTER_MODEL", _DEFAULT_OPENROUTER_MODEL)

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }

    try:
        response = requests.post(
            _OPENROUTER_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=45,
        )
    except requests.RequestException as exc:
        raise ReportGenerationError("OpenRouter request failed.") from exc

    if response.status_code != 200:
        detail = response.text.strip()[:500]
        raise ReportGenerationError(
            f"OpenRouter returned HTTP {response.status_code}: {detail or 'no response body'}"
        )

    try:
        body = response.json()["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, TypeError, ValueError, AttributeError) as exc:
        raise ReportGenerationError("OpenRouter returned an unexpected response format.") from exc

    if not body:
        raise ReportGenerationError("OpenRouter returned an empty report body.")

    return body


def generate_report_email(case_detail: dict[str, Any]) -> dict[str, str]:
    """Use OpenRouter to generate a report body from aggregate case details."""
    inspector_name = _display_value(case_detail.get("leadOfficer", {}).get("inspectorName"))
    upload_id = _display_value(case_detail.get("uploadId"))
    body = request_openrouter_completion(
        (
            "Write a concise, professional internal fraud-case report email body. "
            "Use the supplied aggregate facts only. Do not include a subject line, "
            "greeting is optional, and do not invent facts."
        ),
        _case_summary(case_detail),
    )

    return {
        "subject": f"Fraud Case Report - {inspector_name} - {upload_id}",
        "body": body,
    }


def send_report_email(subject: str, body: str) -> None:
    """Send one generated report to the configured backend-only recipient."""
    host = _required_environment("SMTP_HOST")
    username = _required_environment("SMTP_USERNAME")
    password = _required_environment("SMTP_PASSWORD")
    from_address = _required_environment("SMTP_FROM_ADDRESS")
    recipient = _required_environment("REPORT_RECIPIENT_EMAIL")
    try:
        port = int(os.getenv("SMTP_PORT", "587"))
    except ValueError as exc:
        raise ReportDeliveryError("SMTP_PORT must be a valid integer.") from exc

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = from_address
    message["To"] = recipient
    message.set_content(body)

    try:
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=30) as server:
                server.login(username, password)
                server.send_message(message)
        else:
            with smtplib.SMTP(host, port, timeout=30) as server:
                server.ehlo()
                server.starttls()
                server.ehlo()
                server.login(username, password)
                server.send_message(message)
    except (OSError, smtplib.SMTPException) as exc:
        raise ReportDeliveryError("SMTP delivery failed.") from exc
