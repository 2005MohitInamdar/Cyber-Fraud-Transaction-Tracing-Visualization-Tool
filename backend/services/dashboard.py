"""Database-backed dashboard summaries for the authenticated user."""

from db.connection import get_connection


_USER_UPLOAD_SUMMARY = """
    SELECT
        COUNT(DISTINCT fraud_case_uploads.upload_id) AS total_fraud_cases,
        COUNT(DISTINCT CASE
            WHEN file_upload_sessions.upload_id IS NULL
              OR file_upload_sessions.status <> 'complete'
            THEN fraud_case_uploads.upload_id
        END) AS uploads_in_progress,
        COUNT(DISTINCT CASE
            WHEN file_upload_sessions.status = 'complete'
            THEN fraud_case_uploads.upload_id
        END) AS completed_uploads,
        COUNT(DISTINCT CASE
            WHEN file_upload_sessions.status = 'failed'
            THEN fraud_case_uploads.upload_id
        END) AS failed_uploads
    FROM fraud_case_uploads
    LEFT JOIN file_upload_sessions
        ON file_upload_sessions.upload_id = fraud_case_uploads.upload_id
       AND file_upload_sessions.supabase_user_id = fraud_case_uploads.supabase_user_id
    WHERE fraud_case_uploads.supabase_user_id = %s
"""

_USER_CASES = """
    SELECT
        upload_id AS uploadId,
        inspector_name AS inspectorName,
        created_at AS createdAt
    FROM fraud_case_uploads
    WHERE supabase_user_id = %s
    ORDER BY created_at DESC, id DESC
"""


def get_dashboard_summary(user_id: str) -> dict:
    """Return distinct-upload counts visible only to ``user_id``."""
    with get_connection() as conn:
        with conn.cursor(dictionary=True) as cur:
            cur.execute(_USER_UPLOAD_SUMMARY, (user_id,))
            summary = cur.fetchone() or {}

    return {
        "totalFraudCases": int(summary.get("total_fraud_cases") or 0),
        "uploadsInProgress": int(summary.get("uploads_in_progress") or 0),
        "completedUploads": int(summary.get("completed_uploads") or 0),
        "failedUploads": int(summary.get("failed_uploads") or 0),
    }


def get_user_cases(user_id: str) -> list[dict]:
    """Return only the current user's cases, newest first."""
    with get_connection() as conn:
        with conn.cursor(dictionary=True) as cur:
            cur.execute(_USER_CASES, (user_id,))
            return cur.fetchall()
