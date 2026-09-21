"""Generate, validate, scope, and execute safe single-table case searches."""

import re
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import mysql.connector

from db.connection import get_connection
from services.caseReport.report_email import request_openrouter_completion


ALLOWED_TABLES = frozenset({
    "amount_summary",
    "complaint_meta",
    "complaint_transactions",
    "failed_transactions",
    "hold_accounts",
    "lien_transactions",
    "no_action_references",
    "pending_transactions",
})

TABLE_COLUMNS = {
    "amount_summary": "amount_summary_id, s_no, description, amount, upload_id, supabase_user_id",
    "complaint_meta": "complaint_meta_id, id, complaint_accepted_by, complaint_accepted_date, current_status, under_process_date, upload_id, supabase_user_id",
    "complaint_transactions": "complaint_transactions_id, s_no, account_wallet_id, transaction_id, card_details, transaction_amount, reference_no, transaction_datetime, complaint_date, bank_fi, upload_id, supabase_user_id",
    "failed_transactions": "failed_transactions_id, s_no, account_no, transaction_date, transaction_amount, reference_remarks, action_taken_by, date_of_action, upload_id, supabase_user_id",
    "hold_accounts": "hold_accounts_id, s_no, account_no, hold_date, hold_amount, reference_remarks, action_taken_by, date_of_action, upload_id, supabase_user_id",
    "lien_transactions": "lien_transactions_id, s_no, bank_fi, account_no, ifsc_code, layer, transaction_id, transaction_datetime, transaction_amount, disputed_amount, reference_remarks, action_taken_by, date_of_action, upload_id, supabase_user_id",
    "no_action_references": "no_action_references_id, s_no, reference_remarks, action_taken_by, date_of_action, upload_id, supabase_user_id",
    "pending_transactions": "pending_transactions_id, s_no, bank, no_of_transactions_pending, amount_pending, pending_from, upload_id, supabase_user_id",
}

_BLOCKED_PATTERN = re.compile(
    r"\b(?:INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE|GRANT|REVOKE|EXEC|EXECUTE|CALL|INTO|LOAD_FILE|OUTFILE|INFORMATION_SCHEMA|JOIN|UNION|SLEEP|BENCHMARK)\b|\bFOR\s+UPDATE\b|\bLOCK\s+IN\s+SHARE\s+MODE\b|--|/\*|#",
    re.IGNORECASE,
)
_FROM_PATTERN = re.compile(r"\bFROM\s+`?([a-zA-Z_][a-zA-Z0-9_]*)`?", re.IGNORECASE)
_SELECT_ALL_PATTERN = re.compile(r"^\s*SELECT\s+\*\s+FROM\s+", re.IGNORECASE)


class UnsafeQueryError(ValueError):
    """A generated statement did not pass the non-negotiable safety checks."""

    def __init__(self, message: str, sql: str):
        super().__init__(message)
        self.sql = sql


class SearchExecutionError(RuntimeError):
    """A safe generated query could not be executed against the database."""

    def __init__(self, sql: str):
        super().__init__("Query execution failed.")
        self.sql = sql


def _strip_markdown_fence(sql: str) -> str:
    cleaned = sql.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:sql)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned).strip()
    return cleaned


def generate_sql_query(natural_language_query: str, upload_id: str, user_id: str) -> str:
    """Ask OpenRouter for SQL without revealing any case or user identifiers."""
    del upload_id, user_id
    schema = "\n".join(f"- {table}: {columns}" for table, columns in TABLE_COLUMNS.items())
    system_prompt = (
        "Output only one MySQL SQL statement, with no markdown or explanation. "
        "It must be exactly a single-table SELECT * FROM query against one of the allowed tables below. "
        "Never use JOIN, UNION, subqueries, DDL, DML, comments, or multiple statements. "
        "Include a WHERE clause beginning with upload_id IS NOT NULL; the application will add the real "
        "upload_id and supabase_user_id filters itself, so never guess or include either identifier value. "
        "You may add ordinary predicates after that structural clause to answer the request.\nAllowed schema:\n"
        + schema
    )
    return _strip_markdown_fence(request_openrouter_completion(system_prompt, natural_language_query))


def validate_sql_query(sql: str) -> None:
    """Fail closed unless the statement is one safe, single-table SELECT *."""
    normalized = sql.strip()
    if normalized.endswith(";"):
        normalized = normalized[:-1].rstrip()
    if not normalized:
        raise ValueError("Generated SQL is empty.")
    if ";" in normalized:
        raise ValueError("Only one SQL statement is allowed.")
    if not re.match(r"^\s*SELECT\b", normalized, re.IGNORECASE):
        raise ValueError("Only SELECT statements are allowed.")
    if _BLOCKED_PATTERN.search(normalized):
        raise ValueError("The generated SQL contains a blocked keyword or comment pattern.")
    if not _SELECT_ALL_PATTERN.match(normalized):
        raise ValueError("Search queries must use SELECT * so application scoping remains enforceable.")

    tables = _FROM_PATTERN.findall(normalized)
    if len(tables) != 1:
        raise ValueError("The query must reference exactly one allowed table.")
    if tables[0].lower() not in ALLOWED_TABLES:
        raise ValueError("The query references a table that is not available for case search.")
    from_section = re.split(r"\bWHERE\b", normalized, maxsplit=1, flags=re.IGNORECASE)[0]
    if "," in from_section or "(" in from_section or ")" in from_section:
        raise ValueError("The query must use one plain table reference without joins or subqueries.")
    if not re.search(r"\bWHERE\b", normalized, re.IGNORECASE) or not re.search(
        r"\bupload_id\b", normalized, re.IGNORECASE
    ):
        raise ValueError("The query must include a WHERE clause referencing upload_id.")


def scope_sql_query(sql: str, upload_id: str, user_id: str) -> str:
    """Wrap a validated SELECT * with mandatory parameterized case ownership filters."""
    del upload_id, user_id
    normalized = sql.strip().rstrip(";").rstrip()
    return (
        "SELECT * FROM ("
        + normalized
        + ") AS scoped_subquery "
        "WHERE scoped_subquery.upload_id = %s "
        "AND scoped_subquery.supabase_user_id = %s LIMIT 500"
    )


def _json_value(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value


def execute_search_query(scoped_sql: str, upload_id: str, user_id: str) -> list[dict[str, Any]]:
    """Execute a previously scoped statement using parameterized ownership values."""
    try:
        with get_connection() as conn:
            with conn.cursor(dictionary=True) as cur:
                cur.execute(scoped_sql, (upload_id, user_id))
                return [
                    {key: _json_value(value) for key, value in row.items()}
                    for row in cur.fetchall()
                ]
    except mysql.connector.Error as exc:
        raise RuntimeError("Query execution failed.") from exc


def _ensure_case_ownership(upload_id: str, user_id: str) -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM fraud_case_uploads WHERE upload_id = %s AND supabase_user_id = %s LIMIT 1",
                (upload_id, user_id),
            )
            if cur.fetchone() is None:
                raise PermissionError("You do not have access to this case.")


def run_case_search(natural_language_query: str, upload_id: str, user_id: str) -> dict[str, Any]:
    """Own the case, generate SQL, validate it, then enforce scope before execution."""
    _ensure_case_ownership(upload_id, user_id)
    sql = generate_sql_query(natural_language_query, upload_id, user_id)
    try:
        validate_sql_query(sql)
    except ValueError as exc:
        raise UnsafeQueryError(str(exc), sql) from exc

    scoped_sql = scope_sql_query(sql, upload_id, user_id)
    try:
        rows = execute_search_query(scoped_sql, upload_id, user_id)
    except RuntimeError as exc:
        raise SearchExecutionError(sql) from exc
    return {"sql": sql, "rows": rows}
