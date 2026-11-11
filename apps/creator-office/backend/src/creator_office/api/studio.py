"""Studio: uploads, files, clips, exports, designs and the brand kit."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from creator_office import storage
from creator_office.api import serializers
from creator_office.api.deps import (
    ApiError,
    Auth,
    Current,
    Db,
    Editor,
    current_auth,
    owned,
    project_of,
)
from creator_office.connectors import platform_specs as specs
from creator_office.database import session_scope
from creator_office.media import ffmpeg, subtitles, thumbnails
from creator_office.models import Asset, Clip, Design, Project, Publication, Render
from creator_office.pipeline import engine
from creator_office.pipeline.catalog import FORMATS
from creator_office.services import brand as brand_service
from creator_office.services import production, publishing
from creator_office.settings import get_settings
from creator_office.text import clip_title, decimal, is_auto_clip_title

router = APIRouter(prefix="/api", tags=["studio"])

FFMPEG_NEEDED = (
    "FFmpeg no está disponible en este servidor, así que no se puede exportar aquí. "
    "Alternativa: edita el clip con tu programa y sube la pieza terminada."
)


# --- Files -------------------------------------------------------------------


def _parse_range(header: str, size: int) -> tuple[int, int] | None:
    if not header.startswith("bytes=") or "," in header:
        return None
    first, _, last = header[6:].partition("-")
    try:
        if first == "":
            length = int(last)
            if length <= 0:
                return None
            return max(0, size - length), size - 1
        start = int(first)
        end = int(last) if last else size - 1
    except ValueError:
        return None
    if start >= size or start > end:
        return None
    return start, min(end, size - 1)


@router.get("/files/{asset_id}")
def download(
    asset_id: str, request: Request, exp: int = 0, sig: str = "", download: int = 0
) -> StreamingResponse:
    """Serve a private file to a signed-in member or to the holder of a signed link."""
    # A short session of its own: nothing stays open while the file streams.
    with session_scope() as db:
        asset = db.get(Asset, asset_id)
        signed = bool(sig) and storage.verify_signature(asset_id, exp, sig)
        if not signed:
            auth = current_auth(request, db)
            if asset is None or asset.workspace_id != auth.workspace_id:
                raise ApiError(
                    404,
                    "El archivo no existe o no pertenece a este espacio de trabajo.",
                    "not_found",
                )
        if asset is None:
            raise ApiError(404, "El archivo no existe.", "not_found")
        try:
            path = storage.absolute_path(asset)
        except storage.UploadError as exc:
            raise ApiError(404, "El archivo no existe.", "not_found") from exc
        filename, mime = asset.filename, asset.mime
    if not path.is_file():
        raise ApiError(404, "El archivo ya no está en el almacenamiento.", "not_found")
    size = path.stat().st_size
    headers = {
        "Accept-Ranges": "bytes",
        "Cache-Control": "private, max-age=3600",
        "X-Content-Type-Options": "nosniff",
        "Content-Disposition": f"{'attachment' if download else 'inline'}; filename*=UTF-8''{quote(filename)}",
    }
    byte_range = (
        _parse_range(request.headers.get("range", ""), size)
        if request.headers.get("range")
        else None
    )
    if request.headers.get("range") and byte_range is None:
        return StreamingResponse(
            iter(()), status_code=416, headers={"Content-Range": f"bytes */{size}"}
        )
    start, end = byte_range or (0, size - 1)
    length = end - start + 1 if size else 0
    headers["Content-Length"] = str(length)
    if byte_range:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    return StreamingResponse(
        storage.iter_file(path.open("rb"), start, length),
        status_code=206 if byte_range else 200,
        media_type=mime,
        headers=headers,
    )


# --- Uploads -----------------------------------------------------------------


def _check_image(path: Any) -> None:
    try:
        with Image.open(path) as image:
            image.verify()
    except (UnidentifiedImageError, OSError, SyntaxError) as exc:
        raise ApiError(422, "El archivo no es una imagen válida.", "bad_image") from exc


async def _receive(
    request: Request, workspace_id: str, filename: str, allowed: set[str], what: str
) -> tuple[str, Any, str]:
    """Stream the request body to private storage. Returns (relative path, absolute path, kind)."""
    try:
        kind = storage.kind_for(filename)
    except storage.UploadError as exc:
        raise ApiError(415, str(exc), "bad_type") from exc
    if kind not in allowed:
        raise ApiError(415, f"Aquí hace falta {what}.", "bad_type")
    limit = get_settings().max_upload_bytes
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        raise ApiError(
            413, f"El archivo supera el máximo de {limit // 1_048_576} MB.", "too_large"
        )
    relative, destination = storage.new_path(workspace_id, filename)
    try:
        await storage.save_stream(request.stream(), destination, limit)
    except storage.UploadError as exc:
        raise ApiError(
            413 if "máximo" in str(exc) else 400, str(exc), "upload_failed"
        ) from exc
    return relative, destination, kind


def _probe_or_reject(db: Session, asset: Asset) -> dict[str, Any] | None:
    """Read the real properties of an uploaded video; refuse files FFmpeg cannot open."""
    if ffmpeg.locate() is None:
        return None
    try:
        media = production.media_info(db, asset)
    except ffmpeg.FFmpegError as exc:
        production.delete_asset(db, asset)
        raise ApiError(
            422,
            "No se pudo leer el vídeo: el archivo está dañado o usa un formato no compatible.",
            "bad_video",
        ) from exc
    if not media.get("video"):
        production.delete_asset(db, asset)
        raise ApiError(422, "El archivo no contiene una pista de vídeo.", "bad_video")
    return media


def _store_source(
    db: Session,
    auth: Auth,
    project: Project,
    relative: str,
    filename: str,
    rights: bool,
    note: str,
) -> None:
    has_exports = (
        db.scalar(select(Render.id).where(Render.project_id == project.id).limit(1))
        is not None
    )
    if project.source_asset_id and has_exports:
        (get_settings().storage_dir / relative).unlink(missing_ok=True)
        raise ApiError(
            409,
            "Este proyecto ya tiene piezas exportadas del vídeo actual. Para trabajar con otro vídeo, crea un proyecto nuevo.",
            "source_in_use",
        )
    asset = storage.register_file(
        db,
        workspace_id=auth.workspace_id,
        relative_path=relative,
        filename=filename,
        kind="video",
        origin="upload",
        created_by=auth.user.id,
        project_id=project.id,
        rights_declared=rights,
        rights_note=note,
    )
    media = _probe_or_reject(db, asset)
    if media is not None:
        production.make_poster(db, asset)
    if project.source_asset_id:
        previous = db.get(Asset, project.source_asset_id)
        for clip in db.scalars(select(Clip).where(Clip.project_id == project.id)):
            db.delete(clip)
        if previous is not None:
            production.delete_asset(db, previous)
    project.source_asset_id = asset.id
    step = engine.steps_of(db, project.id)["clips"]
    if step.status in ("completed", "awaiting_approval", "error", "blocked"):
        engine.stop_runs(db, step)
        (
            step.status,
            step.blocked_code,
            step.blocked_reason,
            step.error,
            step.summary,
        ) = "pending", "", "", "", ""
    detail = ""
    if media:
        video = media["video"]
        detail = f" ({video['width']}×{video['height']}, {media['duration']:.0f} s)"
    auth.log(
        db,
        "asset.uploaded",
        f"{auth.user.name} subió el vídeo «{asset.filename}»{detail}.",
        project_id=project.id,
        data={"asset_id": asset.id, "rights_declared": rights},
    )
    engine.advance(db, project)


def _store_subtitles(
    db: Session, auth: Auth, project: Project, relative: str, filename: str
) -> None:
    path = get_settings().storage_dir / relative
    try:
        raw = path.read_bytes()
    finally:
        path.unlink(missing_ok=True)
    video = db.get(Asset, project.source_asset_id) if project.source_asset_id else None
    if video is None:
        raise ApiError(
            409, "Sube primero el vídeo: los subtítulos se asocian a él.", "no_source"
        )
    text = raw.decode("utf-8-sig", errors="replace")
    segments = subtitles.parse(text)
    if not segments:
        raise ApiError(
            422,
            "No se encontró ninguna línea con marcas de tiempo. Usa un archivo .srt o .vtt.",
            "bad_subtitles",
        )
    video.meta = {
        **(video.meta or {}),
        "transcript": {
            "source": "upload",
            "filename": storage.clean_filename(filename),
            "segments": [
                {"start": s.start, "end": s.end, "text": s.text} for s in segments
            ],
        },
    }
    step = engine.steps_of(db, project.id)["clips"]
    if step.status in ("completed", "awaiting_approval") and not step.stale_reason:
        step.stale_reason = "Hay subtítulos nuevos: vuelve a lanzar a Clipper para que tenga en cuenta lo que se dice."
    auth.log(
        db,
        "asset.subtitles",
        f"{auth.user.name} subió subtítulos ({len(segments)} líneas) para «{video.filename}».",
        project_id=project.id,
        data={"segments": len(segments)},
    )


def _store_image(
    db: Session, auth: Auth, project: Project | None, relative: str, filename: str
) -> Asset:
    path = get_settings().storage_dir / relative
    try:
        _check_image(path)
    except ApiError:
        path.unlink(missing_ok=True)
        raise
    return storage.register_file(
        db,
        workspace_id=auth.workspace_id,
        relative_path=relative,
        filename=filename,
        kind="image",
        origin="upload",
        created_by=auth.user.id,
        project_id=project.id if project else None,
        rights_declared=True,
    )


def _store_piece(
    db: Session,
    auth: Auth,
    project: Project,
    relative: str,
    filename: str,
    clip_id: str,
    piece_format: str,
    rights: bool,
) -> None:
    """A piece edited outside the app (the manual alternative to the Editor's export)."""
    path = get_settings().storage_dir / relative
    if piece_format not in FORMATS:
        path.unlink(missing_ok=True)
        raise ApiError(
            422,
            "Indica el formato de la pieza: vertical, horizontal o cuadrado.",
            "bad_format",
        )
    if not rights:
        path.unlink(missing_ok=True)
        raise ApiError(
            422,
            "Confirma que tienes derechos de uso sobre esta pieza.",
            "missing_rights",
        )
    clip = None
    if clip_id:
        clip = db.get(Clip, clip_id)
        if clip is None or clip.project_id != project.id:
            path.unlink(missing_ok=True)
            raise ApiError(404, "El clip no existe en este proyecto.", "not_found")
    export = storage.register_file(
        db,
        workspace_id=auth.workspace_id,
        relative_path=relative,
        filename=filename,
        kind="export",
        origin="upload",
        created_by=auth.user.id,
        project_id=project.id,
        rights_declared=True,
    )
    media = _probe_or_reject(db, export)
    seconds = float((media or {}).get("duration") or 0)
    if media is not None:
        production.make_poster(db, export, at=min(1.0, seconds / 2))
    if clip is None:
        count = len(
            list(db.scalars(select(Clip.id).where(Clip.project_id == project.id)))
        )
        clip = Clip(
            workspace_id=auth.workspace_id,
            project_id=project.id,
            asset_id=export.id,
            start=0.0,
            end=seconds,
            title=storage.clean_filename(filename).rsplit(".", 1)[0][:200],
            reason="Pieza subida ya editada.",
            status="selected",
            origin="user",
            position=count,
        )
        db.add(clip)
        db.flush()
    render = db.scalar(
        select(Render).where(Render.clip_id == clip.id, Render.format == piece_format)
    )
    if render is not None and render.status == "rendering":
        production.delete_asset(db, export)
        raise ApiError(
            409,
            "Esta pieza se está exportando ahora mismo. Espera a que termine o cancélala.",
            "busy",
        )
    committed = (
        publishing.committed_reason(db, render_id=render.id)
        if render is not None
        else ""
    )
    if committed:
        production.delete_asset(db, export)
        raise ApiError(409, committed, "in_use")
    replaced = False
    if render is None:
        render = Render(
            workspace_id=auth.workspace_id,
            project_id=project.id,
            clip_id=clip.id,
            format=piece_format,
        )
        db.add(render)
    elif render.asset_id:
        previous = db.get(Asset, render.asset_id)
        if previous is not None:
            production.delete_asset(db, previous)
        replaced = True
    if media is None:
        checks = [
            specs.check(
                "unverified",
                "warn",
                "Sin verificar",
                "FFmpeg no está disponible: no se han podido medir resolución ni duración.",
            )
        ]
    else:
        checks = production.render_checks(
            render_format=piece_format,
            media=media,
            clip_seconds=seconds,
            subtitles_requested=False,
            subtitles_burned=False,
            transcript_available=False,
            platforms=list(project.platforms),
            size_bytes=export.size,
            rights_declared=True,
        )
        checks = [c for c in checks if c["id"] not in ("subtitles", "duration")]
        checks.append(
            specs.check(
                "subtitles",
                "info",
                "Subtítulos",
                "Pieza subida ya editada: los subtítulos no se pueden comprobar.",
            )
        )
    (
        render.asset_id,
        render.status,
        render.origin,
        render.checks,
        render.error,
        render.progress,
    ) = export.id, "ready", "upload", checks, "", None
    render.settings = {"uploaded": True}
    auth.log(
        db,
        "render.uploaded",
        f"{auth.user.name} subió una pieza ya editada ({piece_format}) para «{clip.title}».",
        project_id=project.id,
        data={"render_id": render.id},
    )
    if replaced:
        db.flush()
        publishing.recheck_approvals(
            db, project.id
        )  # approvals were given to the previous file
    engine.mark_stale(db, project, "edit", "Se subió una pieza después de este paso.")
    engine.advance(db, project)


@router.post("/projects/{project_id}/uploads")
async def upload(
    project_id: str,
    request: Request,
    auth: Editor,
    db: Db,
    kind: str,
    filename: str,
    rights: bool = False,
    rights_note: str = "",
    clip_id: str = "",
    format: str = "",
) -> dict[str, Any]:
    """Raw-body upload (the file is the request body), streamed straight to disk."""
    project = project_of(db, auth, project_id)
    rules = {
        "source": ({"video"}, "un archivo de vídeo"),
        "subtitles": ({"subtitle"}, "un archivo de subtítulos .srt o .vtt"),
        "image": ({"image"}, "una imagen png, jpg o webp"),
        "piece": ({"video"}, "un archivo de vídeo"),
    }
    if kind not in rules:
        raise ApiError(422, "Tipo de subida desconocido.", "bad_kind")
    allowed, what = rules[kind]
    relative, _path, _kind = await _receive(
        request, auth.workspace_id, filename, allowed, what
    )

    def finish() -> dict[str, Any]:
        db.expire_all()  # the upload may have taken minutes: reload what was read before it
        if kind == "source":
            _store_source(
                db, auth, project, relative, filename, rights, rights_note.strip()[:500]
            )
        elif kind == "subtitles":
            _store_subtitles(db, auth, project, relative, filename)
        elif kind == "image":
            image = _store_image(db, auth, project, relative, filename)
            return {
                "asset": serializers.asset(image),
                "project": serializers.project_detail(db, project),
            }
        else:
            _store_piece(db, auth, project, relative, filename, clip_id, format, rights)
        return {"project": serializers.project_detail(db, project)}

    return await run_in_threadpool(finish)


class Rights(BaseModel):
    rights_declared: bool
    rights_note: str = Field(default="", max_length=500)


@router.patch("/assets/{asset_id}")
def declare_rights(asset_id: str, body: Rights, auth: Editor, db: Db) -> dict[str, Any]:
    asset = owned(db, auth, Asset, asset_id, "El archivo")
    asset.rights_declared, asset.rights_note = (
        body.rights_declared,
        body.rights_note.strip(),
    )
    verb = "declaró tener" if body.rights_declared else "retiró la declaración de"
    auth.log(
        db,
        "asset.rights",
        f"{auth.user.name} {verb} derechos de uso sobre «{asset.filename}».",
        project_id=asset.project_id,
        data={"asset_id": asset.id, "note": asset.rights_note},
    )
    project = db.get(Project, asset.project_id) if asset.project_id else None
    if project is not None:
        engine.advance(db, project)
        return {"project": serializers.project_detail(db, project)}
    return {"asset": serializers.asset(asset)}


@router.delete("/assets/{asset_id}")
def delete_image(asset_id: str, auth: Editor, db: Db) -> dict[str, bool]:
    asset = owned(db, auth, Asset, asset_id, "El archivo")
    if asset.kind != "image" or asset.origin != "upload":
        raise ApiError(
            409,
            "Este archivo forma parte de un proyecto y se gestiona desde él.",
            "in_use",
        )
    production.delete_asset(db, asset)
    return {"ok": True}


# --- Clips -------------------------------------------------------------------


class ClipInput(BaseModel):
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    title: str = Field(default="", max_length=200)


class ClipPatch(BaseModel):
    status: str | None = None
    title: str | None = Field(default=None, max_length=200)
    start: float | None = Field(default=None, ge=0)
    end: float | None = Field(default=None, gt=0)


def _clip_bounds(
    db: Session, asset_id: str, start: float, end: float
) -> tuple[float, float]:
    asset = db.get(Asset, asset_id)
    duration = (
        float(((asset.meta or {}).get("media") or {}).get("duration") or 0)
        if asset
        else 0
    )
    if duration:
        end = min(end, duration)
    if end - start < 1.0:
        raise ApiError(
            422,
            "El clip debe durar al menos un segundo y terminar después de empezar.",
            "bad_range",
        )
    return round(start, 2), round(end, 2)


def _project_for(db: Session, auth: Auth, project_id: str) -> Project:
    return project_of(db, auth, project_id)


@router.post("/projects/{project_id}/clips")
def create_clip(
    project_id: str, body: ClipInput, auth: Editor, db: Db
) -> dict[str, Any]:
    project = _project_for(db, auth, project_id)
    if not project.source_asset_id:
        raise ApiError(409, "Sube primero el vídeo del proyecto.", "no_source")
    start, end = _clip_bounds(db, project.source_asset_id, body.start, body.end)
    count = len(list(db.scalars(select(Clip.id).where(Clip.project_id == project.id))))
    clip = Clip(
        workspace_id=auth.workspace_id,
        project_id=project.id,
        asset_id=project.source_asset_id,
        start=start,
        end=end,
        title=body.title.strip() or f"Clip propio {count + 1}",
        reason="Tramo marcado a mano.",
        status="selected",
        origin="user",
        position=count,
        excerpt=subtitles.excerpt(
            production.transcript_of(db.get(Asset, project.source_asset_id)), start, end
        ),
    )
    db.add(clip)
    # Editor already delivered its pieces; this clip has none yet, and the drafts do not know about it.
    engine.mark_stale(db, project, "clips", engine.CLIP_CHANGES[0])
    auth.log(
        db,
        "clip.created",
        f"{auth.user.name} marcó un clip propio ({decimal(start)}–{decimal(end)} s).",
        project_id=project.id,
    )
    return serializers.project_detail(db, project)


@router.patch("/clips/{clip_id}")
def update_clip(clip_id: str, body: ClipPatch, auth: Editor, db: Db) -> dict[str, Any]:
    clip = owned(db, auth, Clip, clip_id, "El clip")
    project = _project_for(db, auth, clip.project_id)
    if body.title is not None:
        clip.title = body.title.strip()
    if body.status is not None:
        if body.status not in ("suggested", "selected", "rejected"):
            raise ApiError(422, "Estado de clip desconocido.", "bad_status")
        if (body.status == "selected") != (clip.status == "selected"):
            engine.mark_stale(db, project, "clips", engine.CLIP_CHANGES[1])
        clip.status = body.status
    if body.start is not None or body.end is not None:
        start, end = _clip_bounds(
            db,
            clip.asset_id,
            body.start if body.start is not None else clip.start,
            body.end if body.end is not None else clip.end,
        )
        if (start, end) != (clip.start, clip.end):
            for render_id in db.scalars(
                select(Render.id).where(Render.clip_id == clip.id)
            ):
                committed = publishing.committed_reason(db, render_id=render_id)
                if committed:
                    raise ApiError(
                        409,
                        committed
                        + " Para probar otro corte sin tocar esa publicación, marca un clip nuevo.",
                        "in_use",
                    )
            previous = clip.title
            clip.start, clip.end = start, end
            clip.excerpt = subtitles.excerpt(
                production.transcript_of(db.get(Asset, clip.asset_id)), start, end
            )
            if is_auto_clip_title(clip.title):
                clip.title = clip_title(
                    start, end
                )  # a name that only restates the span follows the span
            stale = 0
            for render in db.scalars(
                select(Render).where(
                    Render.clip_id == clip.id, Render.origin != "upload"
                )
            ):
                if render.status == "ready":
                    render.status = "stale"
                    stale += 1
            # From the Editor on, everything was made with the old cut (the pieces themselves are marked one by one).
            engine.mark_stale(db, project, "clips", engine.CLIP_CHANGES[2])
            auth.log(
                db,
                "clip.trimmed",
                f"{auth.user.name} ajustó el corte de «{previous}» a {decimal(start)}–{decimal(end)} s.",
                project_id=project.id,
                data={"clip_id": clip.id, "stale_renders": stale},
            )
    return serializers.project_detail(db, project)


def _delete_render(db: Session, render: Render) -> None:
    committed = publishing.committed_reason(db, render_id=render.id)
    if committed:
        raise ApiError(409, committed, "in_use")
    sent = db.scalar(
        select(Publication.id)
        .where(
            Publication.render_id == render.id,
            Publication.status.in_(("scheduled", "published")),
        )
        .limit(1)
    )
    if sent is not None:
        raise ApiError(
            409,
            "Esta pieza ya se publicó o está subida a la plataforma: se conserva como registro de lo que salió.",
            "in_use",
        )
    for pub in db.scalars(
        select(Publication).where(Publication.render_id == render.id)
    ):
        db.delete(pub)
    if render.asset_id:
        export = db.get(Asset, render.asset_id)
        if export is not None:
            production.delete_asset(db, export)
    db.delete(render)


@router.delete("/clips/{clip_id}")
def delete_clip(clip_id: str, auth: Editor, db: Db) -> dict[str, Any]:
    clip = owned(db, auth, Clip, clip_id, "El clip")
    project = _project_for(db, auth, clip.project_id)
    for render in list(db.scalars(select(Render).where(Render.clip_id == clip.id))):
        if render.status == "rendering":
            raise ApiError(
                409,
                "Una pieza de este clip se está exportando. Espera a que termine.",
                "busy",
            )
        _delete_render(db, render)
    db.delete(clip)
    auth.log(
        db,
        "clip.deleted",
        f"{auth.user.name} eliminó el clip «{clip.title}».",
        project_id=project.id,
    )
    return serializers.project_detail(db, project)


# --- Exports -----------------------------------------------------------------


class RenderInput(BaseModel):
    format: str
    framing: str = "blur"
    subtitles: bool = False
    offset_x: float = Field(default=0.5, ge=0, le=1)
    offset_y: float = Field(default=0.5, ge=0, le=1)


@router.post("/clips/{clip_id}/renders")
def request_render(
    clip_id: str, body: RenderInput, auth: Editor, db: Db
) -> dict[str, Any]:
    clip = owned(db, auth, Clip, clip_id, "El clip")
    project = _project_for(db, auth, clip.project_id)
    if body.format not in FORMATS or body.framing not in ("crop", "blur", "pad"):
        raise ApiError(422, "Formato o encuadre desconocido.", "bad_format")
    if ffmpeg.locate() is None:
        raise ApiError(409, FFMPEG_NEEDED, "missing_ffmpeg")
    source = db.get(Asset, clip.asset_id)
    if source is None or not source.rights_declared:
        raise ApiError(
            409,
            "Falta declarar los derechos de uso del vídeo original.",
            "missing_rights",
        )
    render = db.scalar(
        select(Render).where(Render.clip_id == clip.id, Render.format == body.format)
    )
    settings = {
        "framing": body.framing,
        "subtitles": body.subtitles,
        "offset_x": body.offset_x,
        "offset_y": body.offset_y,
    }
    if render is None:
        render = Render(
            workspace_id=auth.workspace_id,
            project_id=project.id,
            clip_id=clip.id,
            format=body.format,
            settings=settings,
        )
        db.add(render)
        db.flush()
    else:
        if render.status in ("rendering", "queued"):
            raise ApiError(409, "Esta pieza ya se está exportando.", "busy")
        committed = publishing.committed_reason(db, render_id=render.id)
        if committed:
            raise ApiError(409, committed, "in_use")
        render.settings = {**(render.settings or {}), **settings}
        render.origin = "agent"
    render.status, render.error, render.progress = "queued", "", None
    label = f"{clip.title or 'Clip'} · {body.format}"
    engine.enqueue_job(
        db,
        workspace_id=auth.workspace_id,
        project_id=project.id,
        agent="editor",
        kind="render",
        title=f"Exportar {label}",
        payload={"render_id": render.id, "label": label},
        engine="tool",
        user_id=auth.user.id,
    )
    auth.log(
        db,
        "render.requested",
        f"{auth.user.name} pidió a Editor exportar «{label}».",
        project_id=project.id,
        data={"render_id": render.id, **settings},
    )
    return serializers.project_detail(db, project)


@router.delete("/renders/{render_id}")
def delete_render(render_id: str, auth: Editor, db: Db) -> dict[str, Any]:
    render = owned(db, auth, Render, render_id, "La pieza")
    project = _project_for(db, auth, render.project_id)
    if render.status == "rendering":
        raise ApiError(
            409,
            "Esta pieza se está exportando. Cancela la tarea o espera a que termine.",
            "busy",
        )
    _delete_render(db, render)
    auth.log(
        db,
        "render.deleted",
        f"{auth.user.name} eliminó una pieza exportada.",
        project_id=project.id,
    )
    return serializers.project_detail(db, project)


# --- Designs -----------------------------------------------------------------


class DesignInput(BaseModel):
    kind: str | None = None
    headline: str | None = Field(default=None, max_length=90)
    layout: str | None = None
    font: str | None = None
    background: str | None = None
    background_asset_id: str | None = None
    frame_time: float | None = Field(default=None, ge=0)


def _apply_design(db: Session, auth: Auth, design: Design, body: DesignInput) -> None:
    spec = dict(design.spec or {})
    if body.headline is not None:
        spec["headline"] = " ".join(body.headline.split())
    if body.layout is not None:
        if body.layout not in thumbnails.LAYOUTS:
            raise ApiError(422, "Composición desconocida.", "bad_layout")
        spec["layout"] = body.layout
    if body.font is not None:
        if body.font not in thumbnails.FONT_FILES:
            raise ApiError(422, "Tipografía desconocida.", "bad_font")
        spec["font"] = body.font
    if body.background is not None:
        if body.background not in ("frame", "gradient", "asset"):
            raise ApiError(422, "Fondo desconocido.", "bad_background")
        spec["background"] = body.background
    if body.background_asset_id is not None:
        owned(db, auth, Asset, body.background_asset_id, "La imagen de fondo")
        spec["background_asset_id"] = body.background_asset_id
    if body.frame_time is not None:
        spec["frame_time"] = body.frame_time
    if not spec.get("headline"):
        raise ApiError(422, "Escribe un titular para el diseño.", "empty_headline")
    design.spec = spec


def _compose_now(db: Session, design_id: str) -> None:
    db.commit()  # the composer works in its own transaction and must see the new spec
    result = production.compose_one(design_id)
    db.expire_all()  # the composer changed the design row from another session
    if result.get("status") != "ready":
        raise ApiError(
            422, result.get("error", "No se pudo componer el diseño."), "compose_failed"
        )


@router.post("/projects/{project_id}/designs")
def create_design(
    project_id: str, body: DesignInput, auth: Editor, db: Db
) -> dict[str, Any]:
    project = _project_for(db, auth, project_id)
    if body.kind not in specs.DESIGN_SIZES:
        raise ApiError(422, "Tipo de diseño desconocido.", "bad_kind")
    if (
        db.scalar(
            select(Design.id).where(
                Design.project_id == project.id, Design.kind == body.kind
            )
        )
        is not None
    ):
        raise ApiError(
            409, "Ya existe un diseño de ese tipo en el proyecto: edítalo.", "exists"
        )
    design = Design(
        workspace_id=auth.workspace_id,
        project_id=project.id,
        kind=body.kind,
        spec={"background": "gradient"},
    )
    db.add(design)
    db.flush()
    _apply_design(db, auth, design, body)
    _compose_now(db, design.id)
    auth.log(
        db,
        "design.created",
        f"{auth.user.name} creó un diseño ({body.kind}).",
        project_id=project.id,
    )
    return serializers.project_detail(db, project)


@router.patch("/designs/{design_id}")
def update_design(
    design_id: str, body: DesignInput, auth: Editor, db: Db
) -> dict[str, Any]:
    design = owned(db, auth, Design, design_id, "El diseño")
    project = _project_for(db, auth, design.project_id)
    committed = publishing.committed_reason(db, design_id=design.id)
    if committed:
        raise ApiError(409, committed, "in_use")
    _apply_design(db, auth, design, body)
    _compose_now(db, design.id)
    step = engine.steps_of(db, project.id)["design"]
    if step.status == "completed":
        design.approved = True  # the person editing it is the one who approves
        engine.mark_stale(
            db, project, "design", "Se cambió un diseño después de este paso."
        )
    auth.log(
        db,
        "design.edited",
        f"{auth.user.name} ajustó {'la miniatura' if design.kind == 'thumbnail' else 'la portada'} de «{project.title}».",
        project_id=project.id,
        data={"design_id": design.id},
    )
    return serializers.project_detail(db, project)


# --- Brand kit ---------------------------------------------------------------


def _brand_payload(db: Session, auth: Auth) -> dict[str, Any]:
    brand = brand_service.merged(auth.workspace.brand, auth.workspace.name)
    logo = db.get(Asset, brand["logo_asset_id"]) if brand.get("logo_asset_id") else None
    return {
        "brand": brand,
        "logo_url": serializers.file_url(logo.id) if logo else None,
        "defaults": brand_service.default_brand(auth.workspace.name),
    }


@router.get("/brand")
def get_brand(auth: Current, db: Db) -> dict[str, Any]:
    return _brand_payload(db, auth)


@router.put("/brand")
def put_brand(body: dict[str, Any], auth: Editor, db: Db) -> dict[str, Any]:
    payload = {k: v for k, v in body.items() if k != "logo_asset_id"}
    for url in payload.get("allowed_sources") or []:
        if not str(url).startswith(("http://", "https://")):
            raise ApiError(
                422,
                f"«{str(url)[:80]}» no es una dirección http(s).",
                "bad_source",
                field="allowed_sources",
            )
    auth.workspace.brand = brand_service.sanitize(payload, auth.workspace.brand)
    auth.log(db, "brand.updated", f"{auth.user.name} actualizó el kit de marca.")
    return _brand_payload(db, auth)


@router.post("/brand/logo")
async def upload_logo(
    request: Request, auth: Editor, db: Db, filename: str
) -> dict[str, Any]:
    relative, _path, _kind = await _receive(
        request, auth.workspace_id, filename, {"image"}, "una imagen png, jpg o webp"
    )

    def finish() -> dict[str, Any]:
        db.expire_all()
        image = _store_image(db, auth, None, relative, filename)
        brand = brand_service.merged(auth.workspace.brand, auth.workspace.name)
        previous = (
            db.get(Asset, brand["logo_asset_id"])
            if brand.get("logo_asset_id")
            else None
        )
        brand["logo_asset_id"] = image.id
        auth.workspace.brand = brand
        if previous is not None:
            production.delete_asset(db, previous)
        auth.log(
            db, "brand.logo", f"{auth.user.name} cambió el logotipo del kit de marca."
        )
        return _brand_payload(db, auth)

    return await run_in_threadpool(finish)


@router.delete("/brand/logo")
def delete_logo(auth: Editor, db: Db) -> dict[str, Any]:
    brand = brand_service.merged(auth.workspace.brand, auth.workspace.name)
    logo = db.get(Asset, brand["logo_asset_id"]) if brand.get("logo_asset_id") else None
    brand["logo_asset_id"] = None
    auth.workspace.brand = brand
    if logo is not None:
        production.delete_asset(db, logo)
    return _brand_payload(db, auth)
