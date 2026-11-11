"""Video and design production: the real work behind Clipper, Editor and Designer."""

from __future__ import annotations

import json
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Any

from PIL import Image, UnidentifiedImageError
from sqlalchemy import select
from sqlalchemy.orm import Session

from creator_office import storage
from creator_office.agents.context import RunContext
from creator_office.connectors import platform_specs as specs
from creator_office.database import session_scope
from creator_office.media import ffmpeg, subtitles, thumbnails
from creator_office.media.ffmpeg import Signals
from creator_office.models import Asset, Clip, Design, Project, Render, Workspace
from creator_office.pipeline.catalog import FORMATS, PLATFORMS
from creator_office.services import brand as brand_service
from creator_office.services import publishing
from creator_office.text import decimal

# --- Asset facts -------------------------------------------------------------


def media_info(session: Session, asset: Asset) -> dict[str, Any]:
    if "media" not in (asset.meta or {}):
        info = ffmpeg.probe(storage.absolute_path(asset))
        asset.meta = {**(asset.meta or {}), "media": info}
        session.flush()
    return dict(asset.meta["media"])


def transcript_of(asset: Asset) -> list[subtitles.Segment]:
    data = (asset.meta or {}).get("transcript") or {}
    return [
        subtitles.Segment(float(s["start"]), float(s["end"]), str(s["text"]))
        for s in data.get("segments", [])
    ]


def _signals_file(asset: Asset) -> Path:
    path = storage.absolute_path(asset)
    return path.with_name(path.name + ".signals.json")


def load_signals(asset: Asset) -> Signals | None:
    path = _signals_file(asset)
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    data["silences"] = [tuple(pair) for pair in data.get("silences", [])]
    return Signals(**data)


def save_signals(asset: Asset, signals: Signals) -> None:
    _signals_file(asset).write_text(json.dumps(asdict(signals)), encoding="utf-8")


def make_poster(session: Session, asset: Asset, at: float | None = None) -> str | None:
    """Store a preview frame for a video asset; returns the poster asset id."""
    media = (asset.meta or {}).get("media") or {}
    if not media.get("video") or ffmpeg.locate() is None:
        return None
    duration = float(media.get("duration") or 0)
    relative, absolute = storage.new_path(asset.workspace_id, "poster.jpg")
    try:
        ffmpeg.extract_frame(
            storage.absolute_path(asset),
            at if at is not None else min(duration * 0.1, 5.0),
            absolute,
            width=640,
        )
    except ffmpeg.FFmpegError:
        absolute.unlink(missing_ok=True)
        return None
    poster = storage.register_file(
        session,
        workspace_id=asset.workspace_id,
        relative_path=relative,
        filename=f"poster-{asset.id}.jpg",
        kind="poster",
        origin="system",
        created_by="system",
        project_id=asset.project_id,
    )
    asset.meta = {**(asset.meta or {}), "poster_asset_id": poster.id}
    session.flush()
    return poster.id


def formats_for(platforms: list[str]) -> list[str]:
    out: list[str] = []
    for platform in platforms:
        fmt = PLATFORMS[platform]["format"]
        if fmt not in out:
            out.append(fmt)
    return out


def pieces_pending(session: Session, project: Project) -> int:
    """Selected clips that lack an up-to-date piece in some format the project's platforms need."""
    formats = formats_for(project.platforms)
    ready: set[tuple[str, str]] = {
        (r.clip_id, r.format)
        for r in session.scalars(
            select(Render).where(
                Render.project_id == project.id, Render.status == "ready"
            )
        )
    }
    clips = session.scalars(
        select(Clip.id).where(Clip.project_id == project.id, Clip.status == "selected")
    )
    return sum(
        1 for clip_id in clips if any((clip_id, fmt) not in ready for fmt in formats)
    )


# --- Rendering ---------------------------------------------------------------


def render_checks(
    *,
    render_format: str,
    media: dict[str, Any],
    clip_seconds: float,
    subtitles_requested: bool,
    subtitles_burned: bool,
    transcript_available: bool,
    platforms: list[str],
    size_bytes: int,
    rights_declared: bool,
) -> list[specs.Check]:
    width, height = FORMATS[render_format]
    video = media.get("video") or {}
    checks: list[specs.Check] = []
    if (video.get("width"), video.get("height")) == (width, height):
        checks.append(
            specs.check(
                "resolution", "pass", "Resolución", f"{width}×{height} px, H.264"
            )
        )
    else:
        checks.append(
            specs.check(
                "resolution",
                "fail",
                "Resolución",
                f"Se esperaba {width}×{height} y se obtuvo {video.get('width')}×{video.get('height')}.",
            )
        )
    duration = float(media.get("duration") or 0)
    if abs(duration - clip_seconds) <= 0.5:
        checks.append(
            specs.check("duration", "pass", "Duración", f"{decimal(duration)} s")
        )
    else:
        checks.append(
            specs.check(
                "duration",
                "warn",
                "Duración",
                f"El archivo dura {decimal(duration)} s y el clip {decimal(clip_seconds)} s.",
            )
        )
    if media.get("audio"):
        checks.append(
            specs.check(
                "audio",
                "pass",
                "Audio",
                "AAC estéreo a 48 kHz (audio original del vídeo).",
            )
        )
    else:
        checks.append(
            specs.check(
                "audio", "warn", "Audio", "El vídeo original no tiene pista de audio."
            )
        )
    if subtitles_burned:
        checks.append(
            specs.check(
                "subtitles",
                "pass",
                "Subtítulos",
                "Incrustados dentro de la zona segura.",
            )
        )
    elif subtitles_requested and not transcript_available:
        checks.append(
            specs.check(
                "subtitles",
                "warn",
                "Subtítulos",
                "No hay transcripción ni archivo de subtítulos para este vídeo: se exportó sin subtítulos.",
            )
        )
    elif subtitles_requested:
        checks.append(
            specs.check(
                "subtitles",
                "warn",
                "Subtítulos",
                "La transcripción no tiene texto en este tramo.",
            )
        )
    else:
        checks.append(
            specs.check(
                "subtitles", "info", "Subtítulos", "No solicitados para esta pieza."
            )
        )
    for platform in platforms:
        if PLATFORMS[platform]["format"] != render_format:
            continue
        for item in specs.validate_video(platform, media, size_bytes):
            if item["id"] == "duration":
                checks.append(
                    specs.check(
                        f"{platform}_duration",
                        item["status"],
                        f"Límites de {PLATFORMS[platform]['name']}",
                        item["detail"],
                    )
                )
    checks.append(
        specs.check(
            "rights",
            "pass",
            "Derechos",
            "Declaraste tener derechos sobre el vídeo original.",
        )
        if rights_declared
        else specs.check(
            "rights",
            "fail",
            "Derechos",
            "Falta declarar los derechos de uso del vídeo original.",
        )
    )
    return checks


def render_one(
    ctx: RunContext | None,
    render_id: str,
    *,
    base: float = 0.0,
    span: float = 1.0,
    label: str = "",
) -> dict[str, Any]:
    """Render one piece with FFmpeg and record the measured result."""
    with session_scope() as session:
        render = session.get(Render, render_id)
        if render is None:
            return {
                "render_id": render_id,
                "status": "error",
                "error": "La pieza ya no existe.",
            }
        clip = session.get(Clip, render.clip_id)
        asset = session.get(Asset, clip.asset_id) if clip else None
        project = session.get(Project, render.project_id)
        workspace = session.get(Workspace, render.workspace_id)
        if clip is None or asset is None or project is None or workspace is None:
            render.status, render.error = "error", "Falta el clip o el vídeo original."
            return {"render_id": render_id, "status": "error", "error": render.error}
        media = media_info(session, asset)
        settings = dict(render.settings or {})
        source = storage.absolute_path(asset)
        segments = subtitles.window(transcript_of(asset), clip.start, clip.end)
        transcript_available = bool(transcript_of(asset))
        brand = brand_service.merged(workspace.brand, workspace.name)
        start, end = clip.start, clip.end
        render_format = render.format
        platforms = list(project.platforms)
        workspace_id, project_id = render.workspace_id, render.project_id
        previous_asset_id = render.asset_id
        rights = asset.rights_declared
        clip_title = clip.title or f"clip-{clip.position + 1}"
        render.status, render.progress, render.error = "rendering", 0.0, ""

    target = FORMATS[render_format]
    want_subtitles = bool(settings.get("subtitles"))
    ass = None
    if want_subtitles and segments:
        safe = specs.SAFE_ZONES[
            "vertical" if render_format == "vertical" else render_format
        ]
        ass = subtitles.to_ass(
            segments,
            size=target,
            font_name=thumbnails.ass_font_name(brand["font"]),
            safe=safe,
        )
    relative, destination = storage.new_path(workspace_id, "render.mp4")

    def on_progress(fraction: float) -> None:
        if ctx is not None:
            ctx.progress(base + span * fraction, label)

    try:
        ffmpeg.render(
            source,
            destination,
            start=start,
            end=end,
            target=target,
            framing=settings.get("framing", "blur"),
            offset_x=float(settings.get("offset_x", 0.5)),
            offset_y=float(settings.get("offset_y", 0.5)),
            ass_subtitles=ass,
            has_audio=bool(media.get("audio")),
            fps=(media.get("video") or {}).get("fps"),
            on_progress=on_progress,
            cancel=ctx.cancel if ctx is not None else None,
        )
    except ffmpeg.Cancelled:
        destination.unlink(missing_ok=True)
        with session_scope() as session:
            row = session.get(Render, render_id)
            if row is not None:
                row.status, row.progress = (
                    ("ready" if row.asset_id else "pending"),
                    None,
                )
        raise
    except ffmpeg.FFmpegError as exc:
        destination.unlink(missing_ok=True)
        with session_scope() as session:
            row = session.get(Render, render_id)
            if row is not None:
                row.status, row.progress, row.error = "error", None, str(exc)
        return {"render_id": render_id, "status": "error", "error": str(exc)}

    with session_scope() as session:
        row = session.get(Render, render_id)
        if row is None:
            destination.unlink(missing_ok=True)
            return {
                "render_id": render_id,
                "status": "error",
                "error": "La pieza se eliminó durante la exportación.",
            }
        safe_title = (
            "".join(
                ch if ch.isalnum() or ch in " -_" else "_" for ch in clip_title
            ).strip()[:60]
            or "clip"
        )
        export = storage.register_file(
            session,
            workspace_id=workspace_id,
            relative_path=relative,
            filename=f"{safe_title}-{render_format}.mp4",
            kind="export",
            origin="agent",
            created_by="editor",
            project_id=project_id,
        )
        out_media = media_info(session, export)
        make_poster(session, export, at=min(1.0, (end - start) / 2))
        checks = render_checks(
            render_format=render_format,
            media=out_media,
            clip_seconds=end - start,
            subtitles_requested=want_subtitles,
            subtitles_burned=ass is not None,
            transcript_available=transcript_available,
            platforms=platforms,
            size_bytes=export.size,
            rights_declared=rights,
        )
        row.asset_id, row.status, row.progress, row.checks, row.error = (
            export.id,
            "ready",
            None,
            checks,
            "",
        )
        row.settings = {**settings, "subtitles_burned": ass is not None}
        if previous_asset_id:
            old = session.get(Asset, previous_asset_id)
            if old is not None:
                _delete_asset(session, old)
            publishing.recheck_approvals(
                session, project_id
            )  # approvals were given to the previous file
        return {
            "render_id": render_id,
            "status": "ready",
            "format": render_format,
            "seconds": out_media.get("duration"),
            "size_mb": round(export.size / 1_048_576, 2),
            "checks": [
                {"label": c["label"], "status": c["status"], "detail": c["detail"]}
                for c in checks
            ],
        }


def _delete_asset(session: Session, asset: Asset) -> None:
    poster_id = (asset.meta or {}).get("poster_asset_id")
    if poster_id:
        poster = session.get(Asset, poster_id)
        if poster is not None:
            storage.delete_file(poster)
            session.delete(poster)
    storage.delete_file(asset)
    session.delete(asset)


def delete_asset(session: Session, asset: Asset) -> None:
    signals = _signals_file(asset)
    signals.unlink(missing_ok=True)
    _delete_asset(session, asset)


# --- Designs -----------------------------------------------------------------


def _open_image(asset: Asset | None) -> Image.Image | None:
    if asset is None:
        return None
    try:
        with Image.open(storage.absolute_path(asset)) as image:
            image.load()
            return image.copy()
    except (OSError, UnidentifiedImageError, storage.UploadError):
        return None


def default_frame_time(session: Session, project: Project) -> float | None:
    """A frame from inside the first selected clip, else a third into the video."""
    if not project.source_asset_id:
        return None
    asset = session.get(Asset, project.source_asset_id)
    media = (asset.meta or {}).get("media") if asset else None
    if not media or not media.get("video"):
        return None
    clip = session.scalars(
        select(Clip)
        .where(Clip.project_id == project.id, Clip.status == "selected")
        .order_by(Clip.position)
    ).first()
    if clip is not None:
        return round((clip.start + clip.end) / 2, 2)
    return round(float(media.get("duration") or 0) / 3, 2)


def compose_one(design_id: str) -> dict[str, Any]:
    """Compose a thumbnail or cover with the brand kit; measured checks included."""
    with session_scope() as session:
        design = session.get(Design, design_id)
        if design is None:
            return {
                "design_id": design_id,
                "status": "error",
                "error": "El diseño ya no existe.",
            }
        project = session.get(Project, design.project_id)
        workspace = session.get(Workspace, design.workspace_id)
        if project is None or workspace is None:
            return {
                "design_id": design_id,
                "status": "error",
                "error": "Falta el proyecto.",
            }
        brand = brand_service.merged(workspace.brand, workspace.name)
        spec = dict(design.spec or {})
        spec["kind"] = design.kind
        spec["colors"] = brand["colors"]
        spec.setdefault("font", brand["font"])
        spec.setdefault("layout", brand["layout"])
        background: Image.Image | None = None
        note = ""
        choice = spec.get("background", "frame")
        if choice == "asset" and spec.get("background_asset_id"):
            chosen = session.get(Asset, spec["background_asset_id"])
            if chosen is not None and chosen.workspace_id == design.workspace_id:
                background = _open_image(chosen)
            if background is None:
                note = "No se pudo abrir la imagen de fondo elegida; se usó el degradado de marca."
        elif choice == "frame":
            video = (
                session.get(Asset, project.source_asset_id)
                if project.source_asset_id
                else None
            )
            when = spec.get("frame_time")
            if when is None:
                when = default_frame_time(session, project)
            if video is not None and when is not None and ffmpeg.locate() is not None:
                with tempfile.TemporaryDirectory(prefix="co-frame-") as tmp:
                    frame = Path(tmp) / "frame.jpg"
                    try:
                        ffmpeg.extract_frame(
                            storage.absolute_path(video), float(when), frame
                        )
                        with Image.open(frame) as image:
                            image.load()
                            background = image.copy()
                        spec["frame_time"] = float(when)
                    except (ffmpeg.FFmpegError, OSError):
                        note = "No se pudo extraer el fotograma; se usó el degradado de marca."
            else:
                note = "Sin vídeo del que extraer un fotograma: se usó el degradado de marca."
                spec["background"] = "gradient"
        logo = (
            _open_image(session.get(Asset, brand["logo_asset_id"]))
            if brand.get("logo_asset_id")
            else None
        )

        relative, destination = storage.new_path(design.workspace_id, "design.jpg")
        try:
            checks = thumbnails.compose(
                spec, destination, background=background, logo=logo
            )
        except Exception as exc:
            destination.unlink(missing_ok=True)
            design.status, design.error = (
                "error",
                f"No se pudo componer el diseño: {exc}",
            )
            return {"design_id": design_id, "status": "error", "error": design.error}
        if note:
            checks.append(specs.check("background", "info", "Fondo", note))
        asset = storage.register_file(
            session,
            workspace_id=design.workspace_id,
            relative_path=relative,
            filename=("miniatura" if design.kind == "thumbnail" else "portada")
            + f"-{project.title[:40]}.jpg",
            kind="design",
            origin="agent",
            created_by="designer",
            project_id=project.id,
        )
        replaced = bool(design.asset_id)
        if design.asset_id:
            old = session.get(Asset, design.asset_id)
            if old is not None:
                _delete_asset(session, old)
            design.version += 1
        design.asset_id, design.status, design.error = asset.id, "ready", ""
        design.checks = checks
        design.spec = {k: v for k, v in spec.items() if k != "colors"}
        design.approved = False
        if replaced:
            publishing.recheck_approvals(
                session, project.id
            )  # approvals were given to the previous cover
        return {
            "design_id": design.id,
            "status": "ready",
            "kind": design.kind,
            "headline": spec.get("headline", ""),
            "checks": [
                {"label": c["label"], "status": c["status"], "detail": c["detail"]}
                for c in checks
            ],
        }
