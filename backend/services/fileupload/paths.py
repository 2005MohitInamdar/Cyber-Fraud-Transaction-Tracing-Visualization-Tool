# services/fileupload/paths.py
import re
from pathlib import Path, PureWindowsPath
from uuid import UUID


def parse_upload_id(value: str) -> str:
    """Return the canonical lowercase hyphenated UUID, or raise ValueError."""
    try:
        return str(UUID(str(value)))
    except (ValueError, AttributeError, TypeError):
        raise ValueError("Invalid uploadId")


def clean_display_name(name: str) -> str:
    """Keep a harmless name for display only. Never trust it for paths."""
    # PureWindowsPath splits on both / and \, whatever OS you run on
    name = PureWindowsPath(name or "").name
    name = re.sub(r"[^\w.\- ]", "_", name).strip(" .")
    return name[:200] or "upload"


def safe_join(base: Path, *parts: str) -> Path:
    """Join and verify the result is still inside base."""
    base = base.resolve()
    target = base.joinpath(*parts).resolve()
    if not target.is_relative_to(base):
        raise ValueError("Path escapes the upload directory")
    return target