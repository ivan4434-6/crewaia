"""Private file storage.

Files live under ``DATA_DIR/storage/<workspace>/`` with opaque names and are
never served statically: every download goes through the API, either with the
caller's session or with a short-lived signed URL.
"""

from __future__ import annotations

import hashlib
import hmac
import mimetypes
import re
import time
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any, BinaryIO

from sqlalchemy.orm import Session

from creator_office.database import new_id
from creator_office.models import Asset
from creator_office.security import derive_key
from creator_office.settings import get_settings

VIDEO_EXT = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi"}
AUDIO_EXT = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp"}
SUBTITLE_EXT = {".srt", ".vtt"}
ALLOWED_EXT = VIDEO_EXT | AUDIO_EXT | IMAGE_EXT | SUBTITLE_EXT

SIGNED_URL_TTL = 6 * 3600


class UploadError(ValueError):
    pass


def kind_for(filename: str) -> str:
    ext = Path(filename).suffix.lower()
    if ext in VIDEO_EXT:
        return "video"
    if ext in AUDIO_EXT:
        return "audio"
    if ext in IMAGE_EXT:
        return "image"
    if ext in SUBTITLE_EXT:
        return "subtitle"
    raise UploadError(
        "Tipo de archivo no admitido. Vídeo: mp4, mov, m4v, webm, mkv, avi. Audio: mp3, wav, m4a. "
        "Imagen: png, jpg, webp. Subtítulos: srt, vtt."
    )


def clean_filename(name: str) -> str:
    base = Path(name.replace("\\", "/")).name.strip() or "archivo"
    base = re.sub(r"[\x00-\x1f\x7f\"<>|:*?]", "_", base)
    return base[:200]


def workspace_dir(workspace_id: str) -> Path:
    path = get_settings().storage_dir / workspace_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def new_path(workspace_id: str, filename: str) -> tuple[str, Path]:
    """Return (path relative to the storage root, absolute path) for a new file."""
    ext = Path(filename).suffix.lower()
    name = f"{new_id()}{new_id()}{ext}"
    absolute = workspace_dir(workspace_id) / name
    return f"{workspace_id}/{name}", absolute


def absolute_path(asset: Asset) -> Path:
    root = get_settings().storage_dir.resolve()
    path = (root / asset.path).resolve()
    if root not in path.parents:
        raise UploadError("Ruta de archivo no válida.")
    return path


async def save_stream(chunks: AsyncIterator[bytes], dest: Path, limit: int) -> int:
    size = 0
    try:
        with dest.open("wb") as handle:
            async for chunk in chunks:
                size += len(chunk)
                if size > limit:
                    raise UploadError(
                        f"El archivo supera el máximo de {limit // 1_048_576} MB."
                    )
                handle.write(chunk)
    except BaseException:
        dest.unlink(missing_ok=True)  # noqa: ASYNC240 - one local syscall
        raise
    if size == 0:
        dest.unlink(missing_ok=True)  # noqa: ASYNC240
        raise UploadError("El archivo está vacío.")
    return size


def register_file(
    session: Session,
    *,
    workspace_id: str,
    relative_path: str,
    filename: str,
    kind: str,
    origin: str,
    created_by: str,
    project_id: str | None = None,
    meta: dict[str, Any] | None = None,
    rights_declared: bool = False,
    rights_note: str = "",
) -> Asset:
    absolute = get_settings().storage_dir / relative_path
    asset = Asset(
        workspace_id=workspace_id,
        project_id=project_id,
        kind=kind,
        origin=origin,
        created_by=created_by,
        filename=clean_filename(filename),
        mime=mimetypes.guess_type(filename)[0] or "application/octet-stream",
        size=absolute.stat().st_size,
        path=relative_path,
        meta=meta or {},
        rights_declared=rights_declared,
        rights_note=rights_note,
    )
    session.add(asset)
    session.flush()
    return asset


def delete_file(asset: Asset) -> None:
    try:
        absolute_path(asset).unlink(missing_ok=True)
    except UploadError:
        pass


# --- Signed URLs -------------------------------------------------------------


def _signing_key() -> bytes:
    # Derived from the app secret so it survives restarts without another setting.
    return derive_key("asset-url")


def _signature(asset_id: str, expires: int) -> str:
    return hmac.new(
        _signing_key(), f"{asset_id}:{expires}".encode(), hashlib.sha256
    ).hexdigest()[:40]


def signed_path(asset_id: str, ttl: int = SIGNED_URL_TTL) -> str:
    # Rounded so URLs stay stable (and cacheable) for a while.
    expires = (int(time.time()) // 900 + 1) * 900 + ttl
    return f"/api/files/{asset_id}?exp={expires}&sig={_signature(asset_id, expires)}"


def verify_signature(asset_id: str, expires: int, signature: str) -> bool:
    if expires < time.time():
        return False
    return hmac.compare_digest(_signature(asset_id, expires), signature)


def iter_file(
    handle: BinaryIO, start: int, length: int, chunk: int = 256 * 1024
) -> Iterator[bytes]:
    with handle:
        handle.seek(start)
        remaining = length
        while remaining > 0:
            data = handle.read(min(chunk, remaining))
            if not data:
                break
            remaining -= len(data)
            yield data
