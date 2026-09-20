"""
services/fileupload/upload.py
──────────────────────────────
Handles file upload metadata:
  - Validates via Pydantic models
  - Inserts the upload session into MySQL `file_upload_sessions`
  - Stores the full FileUploadMetadata as JSON in Redis
  - Returns the stored session via a SELECT for confirmation
  - Accepts individual chunk uploads, verifies hash, saves chunk bytes to disk
  - When all chunks arrive: assembles the final file, updates MySQL session row

Redis key structure
-------------------
  upload:{upload_id}:metadata → Redis String
      value : JSON string of the full FileUploadMetadata object
              {"uploadId": "...", "fileName": "...", ..., "chunks": [...]}

  upload:{upload_id}:received → Redis Hash
      field : str(chunkNumber)   value : JSON receipt

  TTL: 24 hours (auto-expires if upload is abandoned)

Disk layout
-----------
  uploads/.tmp/{upload_id}/{chunk_number}.bin  ← temp part files
  uploads/{upload_id}_{file_name}              ← final assembled file
"""

import hashlib
import json
import os
import pathlib
import shutil
from pydantic import BaseModel
from typing import List
from fastapi import Request
from db.connection import get_connection, get_redis
from services.pdf_extraction.extractor import extract_tables
from services.pdf_extraction.wrapper import run_pipeline

# ─── Pydantic Models ──────────────────────────────────────────────────────────

class ChunkMetadata(BaseModel):
    chunkNumber: int
    size: int
    hash: str


class FileUploadMetadata(BaseModel):
    uploadId: str
    fileName: str
    fileSize: int
    contentType: str
    chunkSize: int
    totalChunks: int
    fileHash: str
    status: str
    chunks: List[ChunkMetadata]


# ─── Redis key helper ─────────────────────────────────────────────────────────

def _metadata_key(upload_id: str) -> str:
    """Returns the Redis key that holds the full FileUploadMetadata for an upload."""
    return f"upload:{upload_id}:metadata"

CHUNK_TTL_SECONDS = 60 * 60 * 24   # 24 hours

# ─── Disk paths ───────────────────────────────────────────────────────────────

# Resolve relative to this file: backend/services/fileupload/upload.py → backend/uploads
_BACKEND_DIR = pathlib.Path(__file__).resolve().parent.parent.parent
UPLOADS_DIR   = _BACKEND_DIR / "uploads"
CHUNKS_TMP_DIR = UPLOADS_DIR / ".tmp"

# Ensure directories exist at import time
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
CHUNKS_TMP_DIR.mkdir(parents=True, exist_ok=True)


# ─── SQL ──────────────────────────────────────────────────────────────────────

_INSERT_SESSION = """
    INSERT INTO file_upload_sessions
        (supabase_user_id, upload_id, file_name, file_size,
         content_type, chunk_size, total_chunks, received_chunks, file_hash, status)
    VALUES
        (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""

_UPDATE_SESSION = """
    UPDATE file_upload_sessions
    SET
        total_chunks     = %s,
        received_chunks  = %s,
        status           = %s,
        file_path        = %s
    WHERE upload_id = %s
"""

_SELECT_SESSION = """
    SELECT
        id,
        supabase_user_id,
        upload_id,
        file_name,
        file_size,
        content_type,
        chunk_size,
        total_chunks,
        received_chunks,
        file_hash,
        status,
        created_at,
        updated_at
    FROM file_upload_sessions
    WHERE upload_id = %s
    LIMIT 1
"""


# ─── Service Function ─────────────────────────────────────────────────────────

def initiate_upload(metadata: FileUploadMetadata, user_id: str) -> dict:
    """
    Persists the file upload session to MySQL and full metadata to Redis.

    Flow
    ----
    1. INSERT one row into MySQL `file_upload_sessions`
       - total_chunks    → initialised to 0
       - received_chunks → initialised to empty JSON array []
    2. Store the full FileUploadMetadata as a JSON string in Redis
       - Key   : upload:{upload_id}:metadata
       - Value : JSON string of the complete FileUploadMetadata object
                 (includes uploadId, fileName, fileSize, contentType,
                  chunkSize, totalChunks, fileHash, status, and chunks list)
       - TTL   : 24 hours
    3. SELECT the session row back from MySQL to confirm + return to caller

    Parameters
    ----------
    metadata : FileUploadMetadata  – full metadata object from the request body
    user_id  : str                 – Supabase UUID of the authenticated user

    Returns
    -------
    dict with the confirmed MySQL session row and count of chunks cached in Redis
    """

    # ── 1. MySQL: insert upload session ──────────────────────────────────────
    with get_connection() as conn:
        with conn.cursor(dictionary=True) as cur:

            cur.execute(
                _INSERT_SESSION,
                (
                    user_id,
                    metadata.uploadId,
                    metadata.fileName,
                    metadata.fileSize,
                    metadata.contentType,
                    metadata.chunkSize,
                    0,      # total_chunks   → starts at 0
                    "[]",   # received_chunks → empty JSON array
                    metadata.fileHash,
                    metadata.status,
                ),
            )
            session_row_id = cur.lastrowid
            print(f"  ✔  Inserted file_upload_sessions row id={session_row_id}")

            # ── 3. SELECT the session back to confirm ─────────────────────────
            cur.execute(_SELECT_SESSION, (metadata.uploadId,))
            session = cur.fetchone()

    # ── 2. Redis: store full FileUploadMetadata as a JSON string ────────────
    r = get_redis()
    redis_key = _metadata_key(metadata.uploadId)

    # Serialise the entire FileUploadMetadata (including the chunks list) to JSON
    metadata_json = metadata.model_dump_json()

    r.set(redis_key, metadata_json, ex=CHUNK_TTL_SECONDS)  # SET with 24h TTL
    print(f"  ✔  Stored full FileUploadMetadata in Redis key '{redis_key}' (TTL 24h)")

    return {
        "message": "Upload session stored successfully",
        "sessionRowId": session_row_id,
        "chunksInRedis": len(metadata.chunks),
        "session": session,
    }


# ─── Redis key helpers (received chunks) ─────────────────────────────────────

def _received_key(upload_id: str) -> str:
    """Hash that tracks every chunk receipt: field=chunkNumber, value=JSON."""
    return f"upload:{upload_id}:received"


# ─── Chunk-receive Service ────────────────────────────────────────────────────

def receive_chunk(
    request:Request,
    upload_id: str,
    user_id: str,
    chunk_number: int,
    chunk_bytes: bytes,
) -> dict:
    """
    Accepts a single raw chunk, verifies its integrity against the metadata
    stored in Redis, saves the chunk receipt to Redis, and checks if all
    chunks have been received.

    Parameters
    ----------
    upload_id    : str   – UUID that ties this chunk to its upload session
    chunk_number : int   – 1-indexed chunk number sent by the client
    chunk_bytes  : bytes – raw binary content of the chunk

    Returns
    -------
    dict with verification result, chunk metadata, and completion status

    Raises
    ------
    ValueError – if the upload session is not found in Redis
    ValueError – if chunk_number is not found in the session metadata
    ValueError – if the SHA-256 hash of the received bytes doesn't match
    """

    # ── 1. Confirm this authenticated user owns the upload ───────────────────
    with get_connection() as conn:
        with conn.cursor(dictionary=True) as cur:
            cur.execute(_SELECT_SESSION, (upload_id,))
            session = cur.fetchone()

    if session is None:
        raise ValueError(f"No upload session found for uploadId='{upload_id}'.")
    if session["supabase_user_id"] != user_id:
        raise PermissionError("You do not have permission to upload chunks to this case.")

    # ── 2. Load session metadata from Redis ──────────────────────────────────
    r = get_redis()
    redis_key = _metadata_key(upload_id)
    raw = r.get(redis_key)

    if raw is None:
        raise ValueError(
            f"No upload session found in Redis for uploadId='{upload_id}'. "
            "Either it expired or initiate was never called."
        )

    session_meta = FileUploadMetadata.model_validate_json(raw)

    # ── 3. Find expected ChunkMetadata for this chunk number ─────────────────
    expected_chunk = next(
        (c for c in session_meta.chunks if c.chunkNumber == chunk_number),
        None,
    )

    if expected_chunk is None:
        raise ValueError(
            f"Chunk number {chunk_number} not found in session metadata "
            f"for uploadId='{upload_id}'. "
            f"Valid chunks: 1–{session_meta.totalChunks}"
        )

    # ── 4. Verify SHA-256 hash ────────────────────────────────────────────────
    received_hash = hashlib.sha256(chunk_bytes).hexdigest()
    hash_ok = received_hash == expected_chunk.hash

    # ── 5. Print detailed receipt ─────────────────────────────────────────────
    print(f"\n{'─' * 60}")
    print(f"  📦  Chunk received  [{chunk_number}/{session_meta.totalChunks}]")
    print(f"  uploadId     : {upload_id}")
    print(f"  fileName     : {session_meta.fileName}")
    print(f"  chunkNumber  : {chunk_number}")
    print(f"  expectedSize : {expected_chunk.size:,} bytes")
    print(f"  receivedSize : {len(chunk_bytes):,} bytes")
    print(f"  expectedHash : {expected_chunk.hash}")
    print(f"  receivedHash : {received_hash}")
    print(f"  hashMatch    : {'✅ YES' if hash_ok else '❌ NO — CORRUPTED'}")
    print(f"{'─' * 60}\n")

    if not hash_ok:
        raise ValueError(
            f"Hash mismatch for chunk {chunk_number}: "
            f"expected={expected_chunk.hash}, got={received_hash}"
        )

    # ── 6. Save chunk receipt to Redis Hash + raw bytes to temp file ───────────
    #  Redis Hash  : upload:{upload_id}:received
    #    field = str(chunkNumber),  value = JSON receipt
    #  Temp file   : uploads/.tmp/{upload_id}/{chunk_number}.bin
    received_key = _received_key(upload_id)
    receipt = json.dumps({
        "chunkNumber":  chunk_number,
        "size":         len(chunk_bytes),
        "hash":         received_hash,
        "hashVerified": True,
    })
    r.hset(received_key, str(chunk_number), receipt)
    r.expire(received_key, CHUNK_TTL_SECONDS)          # keep TTL in sync

    # Persist raw chunk bytes to disk so we can assemble later
    tmp_dir = CHUNKS_TMP_DIR / upload_id
    tmp_dir.mkdir(parents=True, exist_ok=True)
    chunk_file = tmp_dir / f"{chunk_number}.bin"
    chunk_file.write_bytes(chunk_bytes)
    print(f"  📝  Saved chunk {chunk_number} bytes to temp file: {chunk_file}")

    chunks_received_so_far = r.hlen(received_key)
    print(
        f"  💾  Saved chunk {chunk_number} receipt to Redis "
        f"({chunks_received_so_far}/{session_meta.totalChunks} received so far)"
    )

    # Keep the metadata immutable: it contains the expected chunk count and
    # hashes. Replacing totalChunks with the received count caused multi-chunk
    # uploads to be finalized after only the first few chunks.
    r.expire(redis_key, CHUNK_TTL_SECONDS)

    # ── 7. Check if all chunks are in — assemble + finalise if so ───────────────
    all_done = chunks_received_so_far >= session_meta.totalChunks
    finalise_result: dict = {}

    if all_done:
        print(f"\n{'═' * 60}")
        print(f"  🎉  Yeah!! All {session_meta.totalChunks} chunk(s) received!")
        print(f"  uploadId : {upload_id}")
        print(f"  fileName : {session_meta.fileName}")
        print(f"  fileSize : {session_meta.fileSize:,} bytes")
        print(f"{'═' * 60}\n")

        finalise_result = _assemble_and_finalise(
            request,
            upload_id=upload_id,
            user_id=user_id,
            file_name=session_meta.fileName,
            total_chunks=session_meta.totalChunks,
        )

    return {
        "uploadId":            upload_id,
        "chunkNumber":         chunk_number,
        "size":                len(chunk_bytes),
        "hash":                received_hash,
        "hashVerified":        True,
        "totalChunks":         session_meta.totalChunks,
        "chunksReceivedSoFar": chunks_received_so_far,
        "allChunksReceived":   all_done,
        **finalise_result,
    }


# ─── Assembly + Finalisation ──────────────────────────────────────────────────

def _assemble_and_finalise(
    request:Request,
    upload_id: str,
    user_id: str,
    file_name: str,
    total_chunks: int,
) -> dict:
    """
    Called once all chunks have arrived.

    Steps
    -----
    1. Read each temp part file (uploads/.tmp/{upload_id}/{n}.bin) in order.
    2. Write them sequentially into uploads/{upload_id}_{file_name}.
    3. Delete the temp directory for this upload.
    4. UPDATE file_upload_sessions in MySQL:
           total_chunks    = total_chunks
           received_chunks = JSON array of chunk numbers  e.g. [1, 2, 3]
           status          = 'complete'
           file_path       = relative path of the saved file
    5. SELECT the updated row back and return it.

    Parameters
    ----------
    upload_id    : str – UUID of the upload session
    file_name    : str – original file name (used in the output filename)
    total_chunks : int – expected number of chunks (for validation)

    Returns
    -------
    dict with filePath, mysqlSession, and finalised flag
    """

    # ── 1. Assemble temp parts into final file ────────────────────────────────

    print("\n\n", request , "\n\n")
    tmp_dir = CHUNKS_TMP_DIR / upload_id

    # Normalise filename: replace spaces with underscores so downstream tools
    # (pdfplumber, etc.) don't have issues with spaces in Windows paths.
    safe_file_name = file_name.replace(" ", "_")

    out_name = f"{upload_id}_{safe_file_name}"
    out_path = UPLOADS_DIR / out_name

    print(f"  🔧  Assembling {total_chunks} chunk(s) → {out_path}")

    with open(out_path, "wb") as out_f:
        for chunk_num in range(1, total_chunks + 1):
            part_path = tmp_dir / f"{chunk_num}.bin"
            if not part_path.exists():
                raise FileNotFoundError(
                    f"Temp chunk file missing: {part_path}. "
                    f"Cannot assemble upload '{upload_id}'."
                )
            out_f.write(part_path.read_bytes())
            print(f"      ✔  Appended chunk {chunk_num}")

    print(f"  ✅  File assembled successfully: {out_path}")

    # ── 2. Clean up temp directory ────────────────────────────────────────────
    shutil.rmtree(tmp_dir, ignore_errors=True)
    print(f"  Removed temp directory: {tmp_dir}")

    # ── 3. UPDATE MySQL file_upload_sessions ──────────────────────────────────
    received_chunks_json = json.dumps(list(range(1, total_chunks + 1)))
    # Store path relative to the backend root for portability
    relative_file_path   = str(out_path.relative_to(_BACKEND_DIR)).replace("\\", "/")

    with get_connection() as conn:
        with conn.cursor(dictionary=True) as cur:
            cur.execute(
                _UPDATE_SESSION,
                (
                    total_chunks,            # total_chunks
                    received_chunks_json,    # received_chunks  e.g. "[1,2,3]"
                    "complete",              # status
                    relative_file_path,      # file_path
                    upload_id,               # WHERE upload_id = %s
                ),
            )
            print(
                f"  MySQL updated — upload_id='{upload_id}' "
                f"status=complete, file_path='{relative_file_path}'"
            )

            # ── 4. SELECT the row back for confirmation ───────────────────────
            cur.execute(_SELECT_SESSION, (upload_id,))
            session_row = cur.fetchone()

    # ── 5. PDF extraction pipeline ────────────────────────────────────────────
    # Only run for PDF files. Each upload gets its own output subdirectory
    # under uploads/extracted/<upload_id>/ to avoid collisions between
    # concurrent uploads.
    extraction_result: dict = {}
    if safe_file_name.lower().endswith(".pdf"):
        extracted_dir = UPLOADS_DIR / "extracted" / upload_id
        extracted_dir.mkdir(parents=True, exist_ok=True)
        try:
            print(f"\n  Starting PDF extraction for upload_id='{upload_id}'...")
            # written = extract_tables(
            #     pdf_path=out_path,
            #     output_dir=extracted_dir,
            # )
            written = run_pipeline(
                pdf_path=out_path,
                user_id=user_id,
                upload_id=upload_id,
                output_root=extracted_dir,
            )
            table_count = len(written["mapped"])
            print(f"  PDF extraction complete — {table_count} table(s) written to {extracted_dir}")
            extraction_result = {
                "extractionStatus": "complete",
                "tablesExtracted":  table_count,
                "extractedDir":     str(extracted_dir.relative_to(_BACKEND_DIR)).replace("\\", "/"),
            }
        except Exception as exc:
            import traceback
            print(f"  [EXTRACTION ERROR] {exc}")
            traceback.print_exc()
            extraction_result = {
                "extractionStatus": "failed",
                "extractionError":  str(exc),
            }
    else:
        print(f"  Skipping PDF extraction — file is not a PDF ({file_name})")
        extraction_result = {"extractionStatus": "skipped"}

    return {
        "finalised":    True,
        "filePath":     relative_file_path,
        "mysqlSession": session_row,
        **extraction_result,
    }
