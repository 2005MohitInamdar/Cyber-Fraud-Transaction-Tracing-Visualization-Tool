"""
services/caseLeadData/lead.py
──────────────────────────────
Handles lead-officer data: validates via Pydantic, then persists a new row
into the `fraud_case_uploads` table in MySQL.
"""

import uuid
from pydantic import BaseModel

from db.connection import get_connection


# ─── Pydantic Model ───────────────────────────────────────────────────────────

class CaseLeadOfficer(BaseModel):
    inspectorName: str
    inspectorRank: str
    inspectorBranch: str


# ─── SQL ──────────────────────────────────────────────────────────────────────

_INSERT_FRAUD_CASE = """
    INSERT INTO fraud_case_uploads
        (supabase_user_id, upload_id, inspector_name, inspector_rank, inspector_branch)
    VALUES
        (%s, %s, %s, %s, %s)
"""


# ─── Service Function ─────────────────────────────────────────────────────────

def receive_lead_data(lead: CaseLeadOfficer, user_id: str, upload_id: str) -> dict:
    """
    Persists lead-officer data for a new fraud-case upload.

    Parameters
    ----------
    lead      : CaseLeadOfficer  – validated officer fields from the request body
    user_id   : str              – Supabase UUID of the authenticated user
    upload_id : str              – UUID that identifies this specific upload session
                                   (taken from fileMetadata.uploadId)

    Returns
    -------
    dict with the auto-generated database row `id`.
    """
    print("\n── Lead Officer Data Received ──────────────────────────")
    print(f"  Name   : {lead.inspectorName}")
    print(f"  Rank   : {lead.inspectorRank}")
    print(f"  Branch : {lead.inspectorBranch}")
    print(f"  UserID : {user_id}")
    print(f"  UploadID: {upload_id}")
    print("────────────────────────────────────────────────────────\n")

    # try:
    #     user_id = get_current_user(access_token)
    # except ValueError as e:
    #     raise HTTPException(
    #         status_code=status.HTTP_401_UNAUTHORIZED,
    #         detail=str(e),
    #     )
        
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                _INSERT_FRAUD_CASE,
                (
                    user_id,
                    upload_id,
                    lead.inspectorName,
                    lead.inspectorRank,
                    lead.inspectorBranch,
                ),
            )
            row_id = cur.lastrowid

    print(f"  ✔  Inserted fraud_case_uploads row id={row_id}")
    return {"message": "Lead officer data saved", "rowId": row_id}
