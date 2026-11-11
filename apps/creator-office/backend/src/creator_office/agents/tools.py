"""CrewAI tools: the only way agents touch the outside world.

Each tool does real, measurable work (HTTP reads, FFmpeg, Pillow, database
queries) and returns what it measured. Results are also kept on the run
context so the code that persists a step never has to trust the model's
retelling of them.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from crewai.tools import BaseTool
from pydantic import BaseModel, Field, PrivateAttr
from sqlalchemy import select, update

from creator_office import storage
from creator_office.agents import schemas
from creator_office.agents.context import RunContext
from creator_office.connectors import platform_specs as specs
from creator_office.database import session_scope
from creator_office.events import record_event
from creator_office.media import ffmpeg, sources, subtitles, transcription
from creator_office.media.candidates import build_candidates, describe
from creator_office.models import (
    Asset,
    Clip,
    Comment,
    Design,
    Project,
    Publication,
    Render,
    Run,
    Workspace,
)
from creator_office.pipeline.catalog import AGENTS, PLATFORMS
from creator_office.services import analytics, community, production, publishing
from creator_office.services import brand as brand_service

logger = logging.getLogger(__name__)


class NoArgs(BaseModel):
    """This tool takes no arguments."""


class OfficeTool(BaseTool):
    _ctx: RunContext = PrivateAttr()

    def __init__(self, ctx: RunContext, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._ctx = ctx

    def execute(self, ctx: RunContext, **kwargs: Any) -> Any:
        raise NotImplementedError

    def for_model(self, result: Any) -> Any:
        """Compact view of the result that is sent back to the language model."""
        return result

    def _log(self, type_: str, message: str, **data: Any) -> None:
        ctx = self._ctx
        try:
            with session_scope() as session:
                record_event(
                    session,
                    workspace_id=ctx.workspace_id,
                    project_id=ctx.project_id,
                    step_id=ctx.step_id,
                    run_id=ctx.run_id,
                    actor_type="agent",
                    actor=ctx.agent,
                    type=type_,
                    message=message,
                    data={"tool": self.name, **data},
                )
                if type_ == "agent.tool_started":
                    label = TOOL_LABELS.get(self.name, self.name)
                    session.execute(
                        update(Run)
                        .where(Run.id == ctx.run_id)
                        .values(progress_label=(label[:1].upper() + label[1:])[:240])
                    )
        except Exception:
            logger.exception("Could not record tool activity for %s", self.name)

    def _run(self, **kwargs: Any) -> str:
        ctx = self._ctx
        ctx.check_cancelled()
        name = AGENTS[ctx.agent].name if ctx.agent in AGENTS else "El agente"
        label = TOOL_LABELS.get(self.name, self.name)
        attempt = ctx.tool_calls.count(self.name) + 1
        self._log(
            "agent.tool_started",
            f"{name} empieza a {label}."
            if attempt == 1
            else f"{name} vuelve a {label} (intento {attempt}).",
        )
        try:
            result = self.execute(ctx, **kwargs)
        except ffmpeg.Cancelled:
            raise
        except Exception as exc:
            logger.exception("Tool %s failed", self.name)
            ctx.tool_calls.append(self.name)
            ctx.tool_results[self.name] = {"error": str(exc)}
            self._log("agent.tool_failed", f"{name} no pudo {label}: {str(exc)[:300]}")
            return json.dumps({"error": str(exc)}, ensure_ascii=False)
        ctx.tool_calls.append(self.name)
        ctx.tool_results[self.name] = result
        self._log("agent.tool_finished", f"{name} terminó de {label}.")
        return json.dumps(self.for_model(result), ensure_ascii=False, default=str)


# --- Scout -------------------------------------------------------------------


class FetchSourcesTool(OfficeTool):
    name: str = "fetch_sources"
    description: str = (
        "Reads every source the user allowed for this project (reference links and RSS feeds) and returns, for each item, "
        "its exact url, title, description, publication date if the source declares one, and a text excerpt. "
        "Sources that could not be read are listed with the reason. Takes no arguments."
    )
    args_schema: type[BaseModel] = NoArgs

    def execute(self, ctx: RunContext, **_: Any) -> dict[str, Any]:
        with session_scope() as session:
            project = session.get(Project, ctx.project_id)
            workspace = session.get(Workspace, ctx.workspace_id)
            urls = [
                str(ref.get("url", "")).strip() for ref in (project.references or [])
            ]
            urls += brand_service.merged(workspace.brand).get("allowed_sources", [])
        unique = list(dict.fromkeys(u for u in urls if u))[:12]
        results: list[dict[str, Any]] = []
        with sources.new_client() as client:
            for index, url in enumerate(unique):
                ctx.progress(
                    index / len(unique), f"Leyendo fuente {index + 1} de {len(unique)}"
                )
                results.append(sources.fetch_source(url, client).as_dict())
        if unique:
            ctx.progress(1.0, "Fuentes leídas")
        items = [item for result in results if result["ok"] for item in result["items"]]
        failed = [
            {"url": r["requested_url"], "reason": r["error"]}
            for r in results
            if not r["ok"]
        ]
        return {"requested": unique, "items": items, "failed": failed}

    def for_model(self, result: dict[str, Any]) -> dict[str, Any]:
        return {
            "items": [
                {**item, "excerpt": item["excerpt"][:700]} for item in result["items"]
            ],
            "failed": result["failed"],
            "note": "Only cite urls listed in items. If items is empty, say that no source could be read.",
        }


# --- Writer ------------------------------------------------------------------


class CopyArgs(BaseModel):
    items: list[schemas.PlatformCopy] = Field(description="Platform copy to check.")


class CheckCopyTool(OfficeTool):
    name: str = "check_platform_copy"
    description: str = "Checks titles, descriptions and hashtags against each platform's length and hashtag limits and returns the problems found."
    args_schema: type[BaseModel] = CopyArgs

    def execute(
        self, ctx: RunContext, items: list[Any] | None = None, **_: Any
    ) -> dict[str, Any]:
        report = []
        for raw in items or []:
            item = raw if isinstance(raw, dict) else raw.model_dump()
            platform = item.get("platform", "")
            if platform not in specs.SPECS:
                report.append(
                    {
                        "platform": platform,
                        "status": "fail",
                        "problems": ["Plataforma desconocida."],
                    }
                )
                continue
            title, description = platform_fields(
                platform,
                item.get("title", ""),
                item.get("description", ""),
                item.get("hashtags", []),
            )
            checks = specs.validate_metadata(platform, title, description, [])
            problems = [
                f"{c['label']}: {c['detail']}"
                for c in checks
                if c["status"] in ("fail", "warn")
            ]
            report.append(
                {
                    "platform": platform,
                    "status": specs.overall(checks),
                    "problems": problems,
                }
            )
        return {"report": report}


def platform_fields(
    platform: str, title: str, description: str, hashtags: list[str]
) -> tuple[str, str]:
    """Map generic copy onto the fields a platform actually has."""
    spec = specs.SPECS[platform]
    tags = " ".join(
        tag if tag.startswith("#") else f"#{tag}" for tag in hashtags if tag.strip()
    )
    if spec.description_max == 0:  # TikTok: a single caption
        caption = " ".join(
            part for part in (title.strip() or description.strip(), tags) if part
        )
        return caption, ""
    if spec.title_max == 0:  # Instagram: caption only
        body = description.strip() or title.strip()
        return "", "\n\n".join(part for part in (body, tags) if part)
    return title.strip(), "\n\n".join(
        part for part in (description.strip(), tags) if part
    )


# --- Clipper -----------------------------------------------------------------


class AnalyzeVideoTool(OfficeTool):
    name: str = "analyze_video"
    description: str = (
        "Analyses the video the user uploaded for this project with FFmpeg: loudness, pauses and scene changes, plus the transcript "
        "when subtitles or a transcription service are available. Returns ranked clip candidates with exact timestamps, the measured "
        "signals behind each one and the transcript excerpt if any. Takes no arguments."
    )
    args_schema: type[BaseModel] = NoArgs

    def execute(self, ctx: RunContext, **_: Any) -> dict[str, Any]:
        with session_scope() as session:
            project = session.get(Project, ctx.project_id)
            asset = (
                session.get(Asset, project.source_asset_id)
                if project.source_asset_id
                else None
            )
            if asset is None:
                raise RuntimeError("El proyecto no tiene ningún vídeo subido.")
            if not asset.rights_declared:
                raise RuntimeError("Falta declarar los derechos de uso del vídeo.")
            media = production.media_info(session, asset)
            path = storage.absolute_path(asset)
            signals = production.load_signals(asset)
            segments = production.transcript_of(asset)
            transcript_source = ((asset.meta or {}).get("transcript") or {}).get(
                "source", ""
            )
            options = dict(project.options or {})
            language = project.language
            asset_id = asset.id
        duration = float(media.get("duration") or 0)
        if duration <= 0:
            raise RuntimeError("No se pudo leer la duración del vídeo.")
        if signals is None:
            signals = ffmpeg.analyze(
                path,
                duration=duration,
                has_audio=bool(media.get("audio")),
                has_video=bool(media.get("video")),
                on_progress=lambda f: ctx.progress(
                    f * 0.85, "Midiendo volumen, pausas y cambios de plano"
                ),
                cancel=ctx.cancel,
            )
            with session_scope() as session:
                production.save_signals(session.get(Asset, asset_id), signals)
        transcript_note = ""
        # A transcription that already ran is kept, even when it heard nothing: the same audio is not paid for twice.
        if (
            not segments
            and transcript_source != "api"
            and transcription.status()["configured"]
            and media.get("audio")
        ):
            ctx.progress(0.85, "Transcribiendo el audio")
            try:
                parts = transcription.transcribe(
                    path, duration=duration, language=language, cancel=ctx.cancel
                )
            except transcription.TranscriptionError as exc:
                transcript_note = f"La transcripción falló: {exc}"
            else:
                segments = [
                    subtitles.Segment(p["start"], p["end"], p["text"]) for p in parts
                ]
                transcript_source = "api"
                with session_scope() as session:
                    row = session.get(Asset, asset_id)
                    row.meta = {
                        **(row.meta or {}),
                        "transcript": {
                            "source": "api",
                            "model": transcription.status()["model"],
                            "segments": parts,
                        },
                    }
        if transcript_source == "api" and not segments:
            transcript_note = "La transcripción automática no detectó voz en este vídeo: las sugerencias se basan solo en señales de audio e imagen."
        ctx.progress(0.95, "Buscando los mejores tramos")
        wanted = int(options.get("clip_count", 5))
        candidates, method = build_candidates(
            signals,
            min_seconds=float(options.get("clip_min_s", 15)),
            max_seconds=float(options.get("clip_max_s", 60)),
            limit=max(wanted * 2, wanted + 3),
            transcript=segments,
        )
        ctx.progress(1.0, "Análisis terminado")
        return {
            "asset_id": asset_id,
            "video": {
                "duration": duration,
                **(media.get("video") or {}),
                "has_audio": bool(media.get("audio")),
            },
            "method": method,
            "requested_clips": wanted,
            "transcript": {
                "available": bool(segments),
                "source": transcript_source,
                "note": transcript_note
                or (
                    ""
                    if segments
                    else "Sin transcripción: las sugerencias se basan solo en señales de audio e imagen, no en lo que se dice."
                ),
            },
            "signals": {
                "pauses": len(signals.silences),
                "scene_changes": len(signals.scene_cuts),
                "dynamic_range_db": signals.dynamic_range_db,
            },
            "candidates": [
                {**c.as_dict(), "facts": describe(c.as_dict(), language)}
                for c in candidates
            ],
        }

    def for_model(self, result: dict[str, Any]) -> dict[str, Any]:
        return {
            **{
                k: result[k]
                for k in ("video", "method", "requested_clips", "transcript", "signals")
            },
            "candidates": [
                {
                    "candidate_id": c["id"],
                    "start": c["start"],
                    "end": c["end"],
                    "seconds": c["duration"],
                    "facts": c["facts"],
                    "transcript_excerpt": c["excerpt"],
                }
                for c in result["candidates"]
            ],
        }


# --- Editor ------------------------------------------------------------------


class RenderArgs(BaseModel):
    pieces: list[schemas.RenderPiece] = Field(
        description="One entry per clip and format to export."
    )


class RenderPiecesTool(OfficeTool):
    name: str = "render_pieces"
    description: str = (
        "Exports clips with FFmpeg. For each piece give clip_id, format (vertical 1080x1920, horizontal 1920x1080 or square), "
        "framing (crop = fill the frame cutting the sides, blur = whole picture over a blurred background, pad = black bars) and "
        "whether to burn in subtitles. Returns the measured result and export checks of every piece."
    )
    args_schema: type[BaseModel] = RenderArgs

    def execute(
        self, ctx: RunContext, pieces: list[Any] | None = None, **_: Any
    ) -> dict[str, Any]:
        plan = [p if isinstance(p, dict) else p.model_dump() for p in pieces or []]
        render_ids: list[tuple[str, str]] = []
        skipped: list[str] = []
        with session_scope() as session:
            clips = {
                c.id: c
                for c in session.scalars(
                    select(Clip).where(
                        Clip.project_id == ctx.project_id, Clip.status == "selected"
                    )
                )
            }
            for piece in plan:
                clip = clips.get(piece.get("clip_id", ""))
                if clip is None:
                    skipped.append(
                        f"clip_id desconocido o no seleccionado: {piece.get('clip_id')}"
                    )
                    continue
                fmt = piece.get("format", "vertical")
                render = session.scalar(
                    select(Render).where(
                        Render.clip_id == clip.id, Render.format == fmt
                    )
                )
                settings = {
                    "framing": piece.get("framing", "blur"),
                    "subtitles": bool(piece.get("subtitles", False)),
                    "offset_x": 0.5,
                    "offset_y": 0.5,
                }
                if render is None:
                    render = Render(
                        workspace_id=ctx.workspace_id,
                        project_id=ctx.project_id,
                        clip_id=clip.id,
                        format=fmt,
                        settings=settings,
                    )
                    session.add(render)
                    session.flush()
                elif render.origin == "upload":
                    skipped.append(
                        f"La pieza {fmt} del clip «{clip.title}» la subiste tú: no se sobrescribe."
                    )
                    continue
                elif publishing.committed_reason(session, render_id=render.id):
                    skipped.append(
                        f"La pieza {fmt} del clip «{clip.title}» está en una publicación programada o en curso: no se sobrescribe."
                    )
                    continue
                else:
                    render.settings = {**(render.settings or {}), **settings}
                    render.status = "pending"
                render_ids.append((render.id, f"{clip.title or 'Clip'} · {fmt}"))
        results = []
        for index, (render_id, label) in enumerate(render_ids):
            ctx.check_cancelled()
            results.append(
                production.render_one(
                    ctx,
                    render_id,
                    base=index / len(render_ids),
                    span=1 / len(render_ids),
                    label=f"Exportando {index + 1} de {len(render_ids)}: {label}",
                )
            )
        if render_ids:
            ctx.progress(1.0, "Exportación terminada")
        return {"rendered": results, "skipped": skipped}


# --- Designer ----------------------------------------------------------------


class DesignArgs(BaseModel):
    designs: list[schemas.DesignItem] = Field(description="Designs to compose.")


class ComposeDesignsTool(OfficeTool):
    name: str = "compose_designs"
    description: str = (
        "Composes thumbnails (1280x720) and vertical covers (1080x1920) with the brand kit. For each design give kind, a short "
        "headline, layout (side, band or center) and background (frame = a frame of the uploaded video, gradient = brand colours). "
        "Returns measured checks: text contrast, text size, safe zone and file size."
    )
    args_schema: type[BaseModel] = DesignArgs

    def execute(
        self, ctx: RunContext, designs: list[Any] | None = None, **_: Any
    ) -> dict[str, Any]:
        plan = [d if isinstance(d, dict) else d.model_dump() for d in designs or []]
        ids: list[str] = []
        skipped: list[str] = []
        with session_scope() as session:
            for item in plan:
                kind = item.get("kind", "thumbnail")
                if kind not in specs.DESIGN_SIZES:
                    continue
                spec = {
                    "headline": " ".join(str(item.get("headline", "")).split())[:90],
                    "layout": item.get("layout", "side"),
                    "background": item.get("background", "frame"),
                }
                design = session.scalar(
                    select(Design).where(
                        Design.project_id == ctx.project_id, Design.kind == kind
                    )
                )
                if design is None:
                    design = Design(
                        workspace_id=ctx.workspace_id,
                        project_id=ctx.project_id,
                        kind=kind,
                        spec=spec,
                    )
                    session.add(design)
                    session.flush()
                elif publishing.committed_reason(session, design_id=design.id):
                    skipped.append(
                        f"El diseño «{kind}» está en una publicación programada o en curso: no se sobrescribe."
                    )
                    continue
                else:
                    design.spec = {**(design.spec or {}), **spec, "frame_time": None}
                ids.append(design.id)
        results = []
        for index, design_id in enumerate(ids):
            ctx.progress(
                index / len(ids), f"Componiendo diseño {index + 1} de {len(ids)}"
            )
            results.append(production.compose_one(design_id))
        if ids:
            ctx.progress(1.0, "Diseños compuestos")
        return {"designs": results, "skipped": skipped}


# --- Publisher ---------------------------------------------------------------


class DraftArgs(BaseModel):
    drafts: list[schemas.DraftItem] = Field(
        description="Metadata for each publication draft."
    )


class SaveDraftsTool(OfficeTool):
    name: str = "save_drafts"
    description: str = (
        "Saves title, description and tags for publication drafts and validates each one against its platform's requirements "
        "(lengths, hashtags, video duration and format). Returns the validation result per draft. It never publishes anything."
    )
    args_schema: type[BaseModel] = DraftArgs

    def execute(
        self, ctx: RunContext, drafts: list[Any] | None = None, **_: Any
    ) -> dict[str, Any]:
        plan = [d if isinstance(d, dict) else d.model_dump() for d in drafts or []]
        report = []
        with session_scope() as session:
            for item in plan:
                pub = session.get(Publication, item.get("publication_id", ""))
                if (
                    pub is None
                    or pub.project_id != ctx.project_id
                    or pub.status != "draft"
                ):
                    report.append(
                        {
                            "publication_id": item.get("publication_id"),
                            "status": "fail",
                            "problems": ["Borrador desconocido o ya no editable."],
                        }
                    )
                    continue
                if pub.approved_at is not None:
                    report.append(
                        {
                            "publication_id": pub.id,
                            "status": "skipped",
                            "problems": [
                                "Ya aprobado por una persona: no se modifica."
                            ],
                        }
                    )
                    continue
                spec = specs.SPECS[pub.platform]
                pub.title = str(item.get("title", "")).strip() if spec.title_max else ""
                pub.description = (
                    str(item.get("description", "")).strip()
                    if spec.description_max
                    else ""
                )
                pub.tags = (
                    [str(t).strip() for t in item.get("tags", []) if str(t).strip()][
                        :40
                    ]
                    if spec.tags_total_max
                    else []
                )
                checks = publishing.validate(session, pub)
                publishing.save_version(
                    session,
                    pub,
                    author_type="agent",
                    author="publisher",
                    provider=ctx.payload.get("engine_label", ""),
                )
                report.append(
                    {
                        "publication_id": pub.id,
                        "platform": pub.platform,
                        "status": specs.overall(checks),
                        "problems": [
                            f"{c['label']}: {c['detail']}"
                            for c in checks
                            if c["status"] in ("fail", "warn")
                        ],
                    }
                )
        return {"report": report}


# --- Community ---------------------------------------------------------------


class LoadCommentsTool(OfficeTool):
    name: str = "load_comments"
    description: str = (
        "Returns the comments of this project that have not been triaged yet, each with the category a transparent keyword rule "
        "would assign and the rule that fired. Takes no arguments."
    )
    args_schema: type[BaseModel] = NoArgs

    def execute(self, ctx: RunContext, **_: Any) -> dict[str, Any]:
        with session_scope() as session:
            rows = session.scalars(
                select(Comment)
                .where(Comment.project_id == ctx.project_id, Comment.category == "")
                .order_by(Comment.created_at)
                .limit(80)
            )
            items = []
            for row in rows:
                category, reason = community.classify(row.text)
                items.append(
                    {
                        "comment_id": row.id,
                        "platform": PLATFORMS.get(row.platform, {}).get(
                            "name", row.platform
                        ),
                        "author": row.author,
                        "text": row.text[:1200],
                        "rule_category": category,
                        "rule_reason": reason,
                    }
                )
        return {"comments": items}


# --- Analyst -----------------------------------------------------------------


class ComputeStatsTool(OfficeTool):
    name: str = "compute_stats"
    description: str = (
        "Computes statistics from the metrics that exist for published pieces: per-piece figures with their origin (platform or "
        "typed in by hand), totals, and comparisons by platform, format, topic and time of day. A comparison is only marked "
        "sufficient when at least two groups have three pieces or more. Takes no arguments."
    )
    args_schema: type[BaseModel] = NoArgs

    def execute(self, ctx: RunContext, **_: Any) -> dict[str, Any]:
        with session_scope() as session:
            return analytics.compute(
                session,
                ctx.workspace_id,
                ctx.project_id if ctx.payload.get("scope") == "project" else None,
            )

    def for_model(self, result: dict[str, Any]) -> dict[str, Any]:
        return {
            **{
                k: result[k]
                for k in (
                    "published_total",
                    "with_metrics",
                    "without_metrics",
                    "sources",
                    "incomplete",
                    "totals",
                    "comparisons",
                    "min_group",
                )
            },
            "publications": [
                {
                    k: p[k]
                    for k in (
                        "title",
                        "platform_name",
                        "format",
                        "project",
                        "source",
                        "values",
                        "engagement_rate",
                        "missing",
                    )
                }
                for p in result["publications"][:40]
            ],
        }


TOOLS_BY_STEP: dict[str, list[type[OfficeTool]]] = {
    "research": [FetchSourcesTool],
    "script": [CheckCopyTool],
    "clips": [AnalyzeVideoTool],
    "edit": [RenderPiecesTool],
    "design": [ComposeDesignsTool],
    "publish": [SaveDraftsTool],
    "community": [LoadCommentsTool],
    "analysis": [ComputeStatsTool],
}

TOOL_LABELS = {
    "fetch_sources": "leer las fuentes permitidas",
    "check_platform_copy": "comprobar los límites de texto de cada plataforma",
    "analyze_video": "analizar el vídeo con FFmpeg",
    "render_pieces": "exportar las piezas con FFmpeg",
    "compose_designs": "componer los diseños",
    "save_drafts": "guardar y validar los borradores",
    "load_comments": "leer los comentarios pendientes",
    "compute_stats": "calcular las estadísticas",
}
