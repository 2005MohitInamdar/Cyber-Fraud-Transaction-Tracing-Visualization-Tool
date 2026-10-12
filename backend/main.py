from fastapi import FastAPI, HTTPException, Request, status, Response, UploadFile, File, Form
from pathlib import Path
from uuid import uuid4
import hashlib
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr
from starlette.concurrency import run_in_threadpool
from services.auth import login, signup, get_current_user
from services.fileupload.upload import FileUploadMetadata, initiate_upload, receive_chunk
from services.fileupload.paths import parse_upload_id
from services.caseLeadData.lead import CaseLeadOfficer, receive_lead_data
from services.dashboard import get_dashboard_summary, get_user_cases
from services.upload_progress import get_events_for_user, publish
from services.caseDetail.detail import get_case_detail
from services.caseGraph.graph import get_case_graph
from services.caseReport.report_email import generate_report_email, send_report_email
from services.caseSearch.sql_search import SearchExecutionError, UnsafeQueryError, run_case_search
from services.email.requisition_service import (
    preview_single, send_single, list_requisitions, SinglePreviewRequest, SingleSendRequest,
)
from services.fileupload.paths import safe_join
from services.bankResponse.parse import MAX_RESPONSE_BYTES, parse_response
from services.bankResponse.service import add_row, create_parsed_response, list_responses, update_row
from services.bankResponse.rebuild import rebuild_case

# ─── Cookie config (single source of truth) ──────────────────────────────────
_COOKIE_NAME    = "access_token"
_COOKIE_MAX_AGE = 60 * 60 * 24   # 1 day


class InitiateUploadRequest(BaseModel):
    leadOfficer: CaseLeadOfficer
    fileMetadata: FileUploadMetadata


class CaseSearchRequest(BaseModel):
    query: str


class ResponseRowPatch(BaseModel):
    included: bool | None = None
    data: dict = {}


class ResponseRowCreate(BaseModel):
    tableName: str
    data: dict


class ApplyBankResponseRequest(BaseModel):
    anchorNodeId: str | None = None
    layerShift: int | None = None
    annotation: dict | None = None
    changeNote: str | None = None


app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:4200"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class UserCredentials(BaseModel):
    email: EmailStr
    password: str


@app.get("/health")
def read_root():
    return {"message": "FastAPI backend running"}

@app.post("/auth/createAccount")
def createAccount(credentials: UserCredentials):
    try:
        response = signup(credentials.email, credentials.password)
        print(response)
        
        return {
            "message": "Account created successfully! Please check your email for verification.",
            "email": credentials.email
        }
    except Exception as e:
        print(e)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )




@app.post("/auth/loginUser")
def loginUser(credentials: UserCredentials, response: Response):
    try:
        auth_response = login(credentials.email, credentials.password)
        session = auth_response.session

        if not session:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid email or password."
            )
        
        response.set_cookie(
            key=_COOKIE_NAME,
            value=session.access_token,
            httponly=True,
            secure=False,   # Set to True in production (requires HTTPS)
            samesite="lax",
            max_age=_COOKIE_MAX_AGE,
        )

        return {"message": "Logged in successfully!"}
        
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password.",
        )


# ─── Auth: Session Check & Logout ────────────────────────────────────────────

@app.get("/auth/me", status_code=status.HTTP_200_OK)
def get_me(request: Request):
    """
    Lightweight session check used by the Angular auth guard.

    Reads the HttpOnly `access_token` cookie, validates it with Supabase,
    and returns the authenticated user's UUID.  Returns 401 if the cookie
    is absent or the token is invalid / expired.

    The Angular guard calls this on every protected route activation; it
    MUST be fast (Supabase token validation is a single remote call).
    """
    access_token = request.cookies.get(_COOKIE_NAME)
    try:
        user_id = get_current_user(access_token)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(e),
        )
    return {"userId": user_id}


@app.post("/auth/logout", status_code=status.HTTP_200_OK)
def logout(response: Response):
    """
    Clears the HttpOnly `access_token` cookie.

    The Angular frontend calls this on sign-out.  We do not need to
    invalidate the Supabase session server-side because the token will
    simply expire naturally; clearing the cookie is sufficient to prevent
    further authenticated requests from this browser.
    """
    response.delete_cookie(
        key=_COOKIE_NAME,
        httponly=True,
        samesite="lax",
    )
    return {"message": "Logged out successfully."}


# ─── Dashboard Routes ────────────────────────────────────────────────────────

@app.get("/api/dashboard/summary", status_code=status.HTTP_200_OK)
def dashboard_summary(request: Request):
    """Return per-user, distinct-upload counts for the dashboard."""
    access_token = request.cookies.get(_COOKIE_NAME)
    try:
        user_id = get_current_user(access_token)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(e),
        )

    try:
        return get_dashboard_summary(user_id)
    except Exception as e:
        print(f"[DASHBOARD ERROR] {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to load dashboard data. Please try again.",
        )


@app.get("/api/dashboard/cases", status_code=status.HTTP_200_OK)
def dashboard_cases(request: Request):
    """Return case cards belonging only to the authenticated user."""
    try:
        user_id = get_current_user(request.cookies.get(_COOKIE_NAME))
        return {"cases": get_user_cases(user_id)}
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(e))
    except Exception as e:
        print(f"[DASHBOARD CASES ERROR] {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to load cases. Please try again.",
        )


# ─── Upload Routes ───────────────────────────────────────────────────────────

@app.post("/api/uploads/initiate", status_code=status.HTTP_201_CREATED)
def initiate_upload_route(request: Request, body: InitiateUploadRequest):
    """
    Accepts lead officer details + file upload metadata from the Angular
    client.  Validates the session cookie, prints the officer data, and
    returns a simple success message.

    Auth: reads the HttpOnly `access_token` cookie, validates it with
    Supabase, and extracts the user UUID before doing any DB work.
    """
    # ── 1. Extract & validate the access token from the cookie ──────────────
    access_token = request.cookies.get("access_token")
    try:
        user_id = get_current_user(access_token)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(e)
        )

    # ── 2. Persist lead officer data to MySQL ────────────────────────────────
    try:
        db_result = receive_lead_data(
            lead=body.leadOfficer,
            user_id=user_id,
            upload_id=body.fileMetadata.uploadId,
        )
    except Exception as e:
        print(f"[DB ERROR] {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to save case data. Please try again.",
        )

    # ── 3. Persist file upload metadata + chunks to MySQL ─────────────────
    try:
        upload_result = initiate_upload(
            metadata=body.fileMetadata,
            user_id=user_id,
        )
    except Exception as e:
        print(f"[DB ERROR - upload] {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to save file metadata. Please try again.",
        )

    publish(body.fileMetadata.uploadId, "Case created. Preparing secure upload.")

    # ── 4. Acknowledge upload initiation ────────────────────────────────────
    return {
        "message": "upload complete",
        "userId": user_id,
        "caseRowId": db_result.get("rowId"),
        "sessionRowId": upload_result.get("sessionRowId"),
    }


@app.get("/api/uploads/{upload_id}/progress", status_code=status.HTTP_200_OK)
def upload_progress(request: Request, upload_id: str):
    """Return user-owned processing events for one upload."""
    try:
        user_id = get_current_user(request.cookies.get(_COOKIE_NAME))
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(e))
    try:
        upload_id = parse_upload_id(upload_id)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid uploadId.")
    try:
        return {"events": get_events_for_user(upload_id, user_id)}
    except PermissionError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))


@app.get("/api/cases/{upload_id}", status_code=status.HTTP_200_OK)
def case_detail(request: Request, upload_id: str):
    """Return all persisted data for one user-owned fraud case."""
    try:
        user_id = get_current_user(request.cookies.get(_COOKIE_NAME))
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(e))
    try:
        upload_id = parse_upload_id(upload_id)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid uploadId.")
    try:
        return get_case_detail(user_id, upload_id)
    except PermissionError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))
    except Exception as e:
        print(f"[CASE DETAIL ERROR] {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to load case detail. Please try again.",
        )


@app.get("/api/cases/{upload_id}/graph", status_code=status.HTTP_200_OK)
def case_graph(request: Request, upload_id: str, revision: int | None = None):
    """Return the transaction-flow visualization data for one owned case."""
    try:
        user_id = get_current_user(request.cookies.get(_COOKIE_NAME))
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(e))
    try:
        upload_id = parse_upload_id(upload_id)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid uploadId.")
    try:
        return get_case_graph(user_id, upload_id, revision=revision)
    except PermissionError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))
    except Exception as e:
        print(f"[CASE GRAPH ERROR] {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to load case graph. Please try again.",
        )


@app.post("/api/cases/{upload_id}/report-email", status_code=status.HTTP_200_OK)
def send_case_report_email(request: Request, upload_id: str):
    """Generate and deliver an internal report for one user-owned case."""
    try:
        user_id = get_current_user(request.cookies.get(_COOKIE_NAME))
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(e))
    try:
        upload_id = parse_upload_id(upload_id)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid uploadId.")

    try:
        case_data = get_case_detail(user_id, upload_id)
    except PermissionError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))
    except Exception as e:
        print(f"[CASE REPORT DETAIL ERROR] {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to load case data for the report.",
        )

    try:
        email_content = generate_report_email(case_data)
    except Exception as e:
        print(f"[CASE REPORT GENERATION ERROR] {e}")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to generate report email: {e}",
        )

    try:
        send_report_email(email_content["subject"], email_content["body"])
    except Exception as e:
        print(f"[CASE REPORT DELIVERY ERROR] {e}")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to send report email: {e}",
        )

    return {"message": "Report email sent", "subject": email_content["subject"]}


@app.post("/api/cases/{upload_id}/search", status_code=status.HTTP_200_OK)
def search_case(request: Request, upload_id: str, body: CaseSearchRequest):
    """Run a validated natural-language search against one owned case."""
    try:
        user_id = get_current_user(request.cookies.get(_COOKIE_NAME))
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(e))
    try:
        upload_id = parse_upload_id(upload_id)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid uploadId.")

    if not body.query or not body.query.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Query cannot be empty.",
        )

    try:
        return run_case_search(body.query.strip(), upload_id, user_id)
    except PermissionError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))
    except UnsafeQueryError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"message": str(e), "sql": e.sql},
        )
    except SearchExecutionError as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"message": str(e), "sql": e.sql},
        )
    except Exception as e:
        print(f"[CASE SEARCH ERROR] {e}")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Search could not be completed. Please try again.",
        )


# ─── Chunk Upload Route ───────────────────────────────────────────────────────

@app.post("/api/uploads/chunk", status_code=status.HTTP_200_OK)
async def upload_chunk_route(
    request: Request,
    uploadId: str = Form(...),
    chunkNumber: int = Form(...),
    chunk: UploadFile = File(...),
):
    """
    Accepts a single binary chunk as multipart/form-data.

    Form fields
    -----------
    uploadId    : str  – UUID from the initiate step
    chunkNumber : int  – 1-indexed chunk number
    chunk       : file – raw binary blob for this chunk

    Auth: same HttpOnly cookie as /api/uploads/initiate
    """
    # ── 1. Validate auth cookie ───────────────────────────────────────────────
    access_token = request.cookies.get("access_token")
    try:
        user_id = get_current_user(access_token)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(e),
        )

    try:
        uploadId = parse_upload_id(uploadId)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid uploadId.",
        )

    # ── 2. Read chunk bytes ───────────────────────────────────────────────────
    chunk_bytes = await chunk.read()

    # ── 3. Verify & print chunk metadata ─────────────────────────────────────
    try:
        # File assembly, PDF parsing, and MySQL writes are synchronous and can
        # take time. Keep them off the async event loop so the frontend can
        # continue polling /progress while this final chunk is processed.
        result = await run_in_threadpool(
            receive_chunk,
            request,
            upload_id=uploadId,
            user_id=user_id,
            chunk_number=chunkNumber,
            chunk_bytes=chunk_bytes,
        )
    except PermissionError as e:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(e),
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )
    except Exception as e:
        print(f"[CHUNK ERROR] {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to process chunk. Please try again.",
        )

    return result

# ─── Requisition Email Routes ─────────────────────────────────────────────────

def _auth_and_upload(request: Request, upload_id: str) -> tuple[str, str]:
    try:
        user_id = get_current_user(request.cookies.get(_COOKIE_NAME))
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(e))
    try:
        return user_id, parse_upload_id(upload_id)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid uploadId.")


@app.post("/api/cases/{upload_id}/responses/parse", status_code=status.HTTP_201_CREATED)
async def parse_bank_response(request: Request, upload_id: str, file: UploadFile = File(...), requisitionId: int | None = Form(None)):
    """Store and parse a bank PDF in an isolated temporary pipeline workspace."""
    user_id, upload_id = _auth_and_upload(request, upload_id)
    try:
        content = await file.read(MAX_RESPONSE_BYTES + 1)
        if len(content) > MAX_RESPONSE_BYTES:
            raise HTTPException(status_code=413, detail="Response PDF must be 10 MB or smaller.")
        if not content.startswith(b"%PDF-"):
            raise HTTPException(status_code=400, detail="Response file must be a PDF.")
        response_dir = safe_join(Path(__file__).resolve().parent / "uploads" / "responses", upload_id)
        response_dir.mkdir(parents=True, exist_ok=True)
        stored_path = safe_join(response_dir, f"{uuid4()}.pdf")
        stored_path.write_bytes(content)
        digest = hashlib.sha256(content).hexdigest()
        parsed = await run_in_threadpool(parse_response, stored_path)
        return create_parsed_response(user_id, upload_id, digest, str(stored_path), requisitionId, parsed)
    except FileExistsError as exc:
        raise HTTPException(status_code=409, detail={"message": "This response PDF was already uploaded.", "responseId": int(str(exc))})
    except HTTPException:
        raise
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except Exception as exc:
        print(f"[BANK RESPONSE PARSE ERROR] {exc}")
        raise HTTPException(status_code=500, detail="Failed to parse bank response.")


@app.get("/api/cases/{upload_id}/responses", status_code=status.HTTP_200_OK)
def bank_response_history(request: Request, upload_id: str):
    user_id, upload_id = _auth_and_upload(request, upload_id)
    try:
        return list_responses(user_id, upload_id)
    except Exception as exc:
        print(f"[BANK RESPONSE HISTORY ERROR] {exc}")
        raise HTTPException(status_code=500, detail="Failed to load bank response history.")


@app.patch("/api/cases/{upload_id}/responses/{response_id}/rows/{row_id}")
def patch_bank_response_row(request: Request, upload_id: str, response_id: int, row_id: int, body: ResponseRowPatch):
    user_id, upload_id = _auth_and_upload(request, upload_id)
    try:
        changes = dict(body.data)
        if body.included is not None: changes["included"] = body.included
        return update_row(user_id, upload_id, response_id, row_id, changes)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        print(f"[BANK RESPONSE ROW ERROR] {exc}")
        raise HTTPException(status_code=500, detail="Failed to update bank response row.")


@app.post("/api/cases/{upload_id}/responses/{response_id}/rows", status_code=status.HTTP_201_CREATED)
def add_bank_response_row(request: Request, upload_id: str, response_id: int, body: ResponseRowCreate):
    user_id, upload_id = _auth_and_upload(request, upload_id)
    try:
        return add_row(user_id, upload_id, response_id, body.tableName, body.data)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        print(f"[BANK RESPONSE ADD ROW ERROR] {exc}")
        raise HTTPException(status_code=500, detail="Failed to add bank response row.")


@app.post("/api/cases/{upload_id}/responses/{response_id}/apply", status_code=status.HTTP_200_OK)
async def apply_bank_response(request: Request, upload_id: str, response_id: int, body: ApplyBankResponseRequest):
    user_id, upload_id = _auth_and_upload(request, upload_id)
    try:
        annotation = body.annotation or None
        if annotation and str(annotation.get("kind") or "") not in {"no_onward", "withdrawn", "lien_confirmed"}:
            raise ValueError("Invalid annotation kind.")
        result = await run_in_threadpool(
            rebuild_case, user_id, upload_id, response_id, body.changeNote, annotation,
            body.anchorNodeId, body.layerShift,
        )
        return {"revision": result["revision"], "newNodeCount": result["counts"]["newNodeCount"], "resolvedNodeCount": result["counts"]["resolvedNodeCount"], "newNodeIds": result["newNodeIds"], "resolvedNodeIds": result["resolvedNodeIds"], "warnings": result["warnings"]}
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        if str(exc) == "REBUILD_BUSY": raise HTTPException(status_code=409, detail="A rebuild is already in progress.")
        if str(exc) == "ALREADY_APPLIED": raise HTTPException(status_code=409, detail="This bank response has already been applied.")
        if str(exc).startswith("REBUILD_NODE_COUNT_REGRESSION"):
            print(f"[BANK RESPONSE APPLY ERROR] {exc}")
            raise HTTPException(status_code=500, detail="Rebuilt graph unexpectedly has fewer transactions than the previous revision; no changes were saved.")
        print(f"[BANK RESPONSE APPLY ERROR] {exc}"); raise HTTPException(status_code=500, detail="Failed to rebuild case graph.")
    except Exception as exc:
        print(f"[BANK RESPONSE APPLY ERROR] {exc}")
        raise HTTPException(status_code=500, detail="Failed to rebuild case graph.")


@app.post("/api/cases/{upload_id}/requisitions/preview", status_code=status.HTTP_200_OK)
def requisitions_preview(request: Request, upload_id: str, body: SinglePreviewRequest):
    user_id, upload_id = _auth_and_upload(request, upload_id)
    try:
        return preview_single(user_id, upload_id, body)
    except PermissionError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception as e:
        print(f"[REQUISITION PREVIEW ERROR] {e}")
        raise HTTPException(status_code=500, detail="Failed to generate the email.")



@app.post("/api/cases/{upload_id}/requisitions/send", status_code=status.HTTP_200_OK)
def requisitions_send(request: Request, upload_id: str, body: SingleSendRequest):
    user_id, upload_id = _auth_and_upload(request, upload_id)
    try:
        return send_single(user_id, upload_id, body)
    except PermissionError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception as e:
        print(f"[REQUISITION SEND ERROR] {e}")
        raise HTTPException(status_code=500, detail="Failed to send the email.")


@app.get("/api/cases/{upload_id}/requisitions", status_code=status.HTTP_200_OK)
def requisitions_list(request: Request, upload_id: str):
    user_id, upload_id = _auth_and_upload(request, upload_id)
    try:
        return list_requisitions(user_id, upload_id)
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc))
    except Exception as exc:
        print(f"[REQUISITION LIST ERROR] {exc}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to load requisition history.")
