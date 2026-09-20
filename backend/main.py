from fastapi import FastAPI, HTTPException, Request, status, Response, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr
from starlette.concurrency import run_in_threadpool
from services.auth import login, signup, get_current_user
from services.fileupload.upload import FileUploadMetadata, initiate_upload, receive_chunk
from services.caseLeadData.lead import CaseLeadOfficer, receive_lead_data
from services.dashboard import get_dashboard_summary, get_user_cases
from services.upload_progress import get_events_for_user, publish
from services.caseDetail.detail import get_case_detail

# ─── Cookie config (single source of truth) ──────────────────────────────────
_COOKIE_NAME    = "access_token"
_COOKIE_MAX_AGE = 60 * 60 * 24   # 1 day


class InitiateUploadRequest(BaseModel):
    leadOfficer: CaseLeadOfficer
    fileMetadata: FileUploadMetadata
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
        return {"events": get_events_for_user(upload_id, user_id)}
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(e))
    except PermissionError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))


@app.get("/api/cases/{upload_id}", status_code=status.HTTP_200_OK)
def case_detail(request: Request, upload_id: str):
    """Return all persisted data for one user-owned fraud case."""
    try:
        user_id = get_current_user(request.cookies.get(_COOKIE_NAME))
        return get_case_detail(user_id, upload_id)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(e))
    except PermissionError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))
    except Exception as e:
        print(f"[CASE DETAIL ERROR] {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to load case detail. Please try again.",
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
