"""Security tests for client-controlled upload metadata and filesystem paths."""

from contextlib import nullcontext
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from services.fileupload.paths import clean_display_name, parse_upload_id, safe_join
from services.fileupload.upload import (
    MAX_TOTAL_CHUNKS,
    FileUploadMetadata,
    _assemble_and_finalise,
)


UPLOAD_ID = "12345678-1234-5678-1234-567812345678"


def _metadata(**changes):
    value = {
        "uploadId": UPLOAD_ID,
        "fileName": "case.pdf",
        "fileSize": 1,
        "contentType": "application/pdf",
        "chunkSize": 1,
        "totalChunks": 1,
        "fileHash": "unused",
        "status": "pending",
        "chunks": [{"chunkNumber": 1, "size": 1, "hash": "unused"}],
    }
    value.update(changes)
    return value


@pytest.mark.parametrize("value", ["../../etc/passwd", "/abs/path", "abc", "", "a" * 100])
def test_parse_upload_id_rejects_non_uuids(value: str) -> None:
    with pytest.raises(ValueError, match="Invalid uploadId"):
        parse_upload_id(value)


def test_parse_upload_id_canonicalises_braces_and_case() -> None:
    assert parse_upload_id("{12345678-1234-5678-1234-567812345678}") == UPLOAD_ID
    assert parse_upload_id(UPLOAD_ID.upper()) == UPLOAD_ID


def test_clean_display_name_removes_path_components_and_limits_length() -> None:
    assert clean_display_name("..\\..\\evil.pdf") == "evil.pdf"
    assert clean_display_name("../../evil.pdf") == "evil.pdf"
    assert clean_display_name("") == "upload"
    assert len(clean_display_name("a" * 1000)) <= 200


def test_safe_join_stays_inside_base(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Path escapes the upload directory"):
        safe_join(tmp_path, "..", "outside")
    assert safe_join(tmp_path, "ok").is_relative_to(tmp_path.resolve())


@pytest.mark.parametrize(
    "changes",
    [
        {"uploadId": "not-a-uuid"},
        {"fileSize": 0},
        {"totalChunks": 0},
        {"totalChunks": MAX_TOTAL_CHUNKS + 1},
    ],
)
def test_metadata_rejects_invalid_upload_bounds(changes: dict) -> None:
    with pytest.raises(ValidationError):
        FileUploadMetadata(**_metadata(**changes))


def test_non_pdf_payload_named_pdf_is_deleted_and_marked_failed(tmp_path: Path) -> None:
    from services.fileupload import upload

    uploads_dir = tmp_path / "uploads"
    chunks_dir = uploads_dir / ".tmp"
    chunk_dir = chunks_dir / UPLOAD_ID
    chunk_dir.mkdir(parents=True)
    (chunk_dir / "1.bin").write_bytes(b"not a PDF")

    cursor = MagicMock()
    connection = MagicMock()
    connection.cursor.return_value = nullcontext(cursor)

    with (
        patch.object(upload, "UPLOADS_DIR", uploads_dir),
        patch.object(upload, "CHUNKS_TMP_DIR", chunks_dir),
        patch.object(upload, "get_connection", return_value=nullcontext(connection)) as get_connection,
        patch.object(upload, "get_redis"),
        patch.object(upload, "publish") as publish,
        patch.object(upload, "run_pipeline") as run_pipeline,
        patch("builtins.print"),
    ):
        with pytest.raises(ValueError, match="Uploaded file is not a valid PDF"):
            _assemble_and_finalise(
                request=MagicMock(),
                upload_id=UPLOAD_ID,
                user_id="test-user",
                file_name="x.pdf",
                total_chunks=1,
            )

    assert not (uploads_dir / f"{UPLOAD_ID}.pdf").exists()
    assert not chunk_dir.exists()
    cursor.execute.assert_called_once_with(upload._MARK_FAILED, (UPLOAD_ID,))
    publish.assert_called_with(UPLOAD_ID, "The uploaded file is not a valid PDF.", "failed")
    run_pipeline.assert_not_called()
    get_connection.assert_called_once()
