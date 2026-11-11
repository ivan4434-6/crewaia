"""What each agent is asked to do, and how its answer is checked and stored.

``prepare`` builds the task for CrewAI from the project state. ``apply`` runs
after the agent finishes: it trusts the tools' recorded results over the
model's wording, drops anything that cannot be traced to them, and persists
the outcome.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from creator_office.agents import schemas
from creator_office.agents.context import RunContext
from creator_office.agents.llm import EngineInfo
from creator_office.agents.tools import platform_fields
from creator_office.connectors import platform_specs as specs
from creator_office.media import ffmpeg
from creator_office.models import (
    Asset,
    Clip,
    Comment,
    Metric,
    Project,
    Publication,
    Recommendation,
    Render,
    Step,
    Workspace,
)
from creator_office.pipeline.catalog import LANGUAGES, PLATFORMS, TEMPLATE_BY_ID
from creator_office.services import brand as brand_service
from creator_office.services import documents, production, publishing
from creator_office.text import choose, plural


class StepFailedError(RuntimeError):
    """The step ran but produced nothing usable."""


@dataclass
class Blocked:
    code: str
    reason: str
    action: str = ""  # what the user can do about it


@dataclass
class Prepared:
    description: str
    expected_output: str
    brief: dict[str, Any] = field(default_factory=dict)
    max_iter: int = 6


@dataclass
class Applied:
    summary: str
    output: dict[str, Any] = field(default_factory=dict)


@dataclass
class StepSpec:
    kind: str
    output_model: type[BaseModel]
    precheck: Callable[[Session, Project], Blocked | None]
    prepare: Callable[[Session, Project, Step, Workspace], Prepared]
    apply: Callable[[Session, RunContext, Any, EngineInfo, Project, Step], Applied]


FFMPEG_MISSING = Blocked(
    "missing_ffmpeg",
    "FFmpeg no está disponible en este servidor, así que no se puede analizar ni exportar vídeo aquí.",
    "Instala FFmpeg (o el extra «media» del backend) y reintenta. Alternativa: descarga el material, edítalo con tu programa "
    "y sube las piezas terminadas desde el estudio.",
)


# --- Shared brief ------------------------------------------------------------


def _base(
    session: Session, project: Project, step: Step, workspace: Workspace
) -> tuple[str, dict[str, Any]]:
    brand = brand_service.merged(workspace.brand, workspace.name)
    template = TEMPLATE_BY_ID.get(project.template)
    learnings = list(
        session.scalars(
            select(Recommendation.text)
            .where(
                Recommendation.workspace_id == workspace.id,
                Recommendation.status == "accepted",
            )
            .order_by(Recommendation.created_at.desc())
            .limit(5)
        )
    )
    language = LANGUAGES.get(project.language, project.language)
    lines = [
        "PROJECT BRIEF",
        f"Title: {project.title}",
        f"Topic: {project.topic or '(not given)'}",
        f"Audience: {project.audience or '(not given)'}",
        f"Goal: {project.goal or '(not given)'}",
        f"Tone: {project.tone or '(not given)'}",
        f"Output language: {language}. Everything the user will read must be written in {language}.",
        "Target platforms: "
        + ", ".join(PLATFORMS[p]["name"] for p in project.platforms),
    ]
    if project.deadline:
        lines.append(f"Deadline: {project.deadline.date().isoformat()}")
    lines += ["", brand_service.voice_brief(brand)]
    if learnings:
        lines += ["", "Lessons from earlier projects that the user accepted:"] + [
            f"- {text}" for text in learnings
        ]
    if step.instructions:
        lines += [
            "",
            f"Extra instructions from the user for this step: {step.instructions}",
        ]
    if step.feedback:
        lines += [
            "",
            f"The user reviewed your previous version and asked for these changes: {step.feedback}",
        ]
    data = {
        "title": project.title,
        "topic": project.topic,
        "audience": project.audience,
        "goal": project.goal,
        "tone": project.tone,
        "language": project.language,
        "platforms": list(project.platforms),
        "structure": list(template.structure) if template else [],
        "brand_hashtags": brand["default_hashtags"],
        "brand_layout": brand["layout"],
        "subtitles_default": brand["subtitles_default"],
        "feedback": step.feedback,
    }
    return "\n".join(lines), data


def _no_block(_session: Session, _project: Project) -> Blocked | None:
    return None


def _limits_text(platforms: list[str]) -> str:
    rows = []
    for platform in platforms:
        spec = specs.SPECS[platform]
        parts = []
        if spec.title_max:
            parts.append(
                f"title up to {spec.title_max} characters"
                if spec.description_max
                else f"single caption up to {spec.title_max} characters (put it in title)"
            )
        if spec.description_max:
            parts.append(f"description up to {spec.description_max} characters")
        if spec.hashtags_max:
            parts.append(f"at most {spec.hashtags_max} hashtags")
        rows.append(f"- {platform} ({spec.name}): " + ", ".join(parts))
    return "\n".join(rows)


# --- Scout: research ---------------------------------------------------------


def _prepare_research(
    session: Session, project: Project, step: Step, workspace: Workspace
) -> Prepared:
    text, data = _base(session, project, step, workspace)
    description = f"""{text}

YOUR TASK
1. Call the fetch_sources tool once. It reads the only sources you are allowed to use.
2. Write findings from what the tool returned. Each finding must use the exact url of an item the tool returned, say what that
   source states, and carry a confidence level (alta, media or baja) with the reason. Copy the publication date from the tool;
   use null when the source declares none.
3. Propose three to five content angles for this audience and goal. For each, list in source_urls the findings that support it,
   or leave it empty when no source does.
4. Under "unverified", state plainly everything you could not verify with these sources.

RULES
- Never invent sources, links, dates, figures or trends. Do not use knowledge that is not in the tool result for factual claims.
- If the tool returns no items, return no findings and say so under "unverified"."""
    return Prepared(
        description,
        "A JSON object with angles, findings, keywords, unverified and notes.",
        data,
    )


def _apply_research(
    session: Session,
    ctx: RunContext,
    out: schemas.ResearchOutput,
    engine: EngineInfo,
    project: Project,
    step: Step,
) -> Applied:
    fetched = ctx.tool_results.get("fetch_sources")
    unverified = [str(item).strip() for item in out.unverified if str(item).strip()]
    items: dict[str, dict[str, Any]] = {}
    failed: list[dict[str, str]] = []
    requested: list[str] = []
    if not isinstance(fetched, dict) or fetched.get("error"):
        reason = (
            fetched.get("error")
            if isinstance(fetched, dict)
            else "el agente no llamó a la herramienta"
        )
        unverified.append(f"No se consultó ninguna fuente ({reason}).")
    else:
        items = {item["url"]: item for item in fetched.get("items", [])}
        failed = fetched.get("failed", [])
        requested = fetched.get("requested", [])
        if not requested:
            unverified.append(
                "El proyecto no tiene fuentes permitidas: añade enlaces de referencia o feeds RSS para que Scout pueda investigar."
            )
    for entry in failed:
        unverified.append(f"No se pudo leer {entry['url']}: {entry['reason']}")

    findings = []
    for finding in out.findings:
        item = items.get(finding.url.strip())
        if item is None:
            unverified.append(
                f"Se descartó una afirmación porque su enlace no figura entre las fuentes leídas: «{finding.claim[:160]}» ({finding.url})."
            )
            continue
        confidence, reason = finding.confidence, finding.confidence_reason
        if confidence == "alta" and not item.get("published"):
            confidence, reason = (
                "media",
                (reason + " " if reason else "")
                + "Rebajada a media: la fuente no declara fecha.",
            )
        findings.append(
            {
                "claim": finding.claim.strip(),
                "source_title": item.get("title") or finding.source_title,
                "site": item.get("site", ""),
                "url": item["url"],
                "published": item.get("published"),
                "confidence": confidence,
                "confidence_reason": reason,
            }
        )
    angles = []
    for angle in out.angles[:6]:
        supported = [url for url in angle.source_urls if url in items]
        angles.append(
            {
                "title": angle.title.strip(),
                "rationale": angle.rationale.strip(),
                "audience": angle.audience.strip(),
                "keywords": [k for k in angle.keywords if k][:8],
                "source_urls": supported,
            }
        )
    if not angles:
        raise StepFailedError("Scout no propuso ningún ángulo.")
    if not findings and items:
        unverified.append(
            "Se leyeron fuentes, pero el agente no extrajo de ellas ningún hallazgo utilizable."
        )
    content = {
        "angles": angles,
        "findings": findings,
        "keywords": [k for k in out.keywords if k][:20],
        "unverified": list(dict.fromkeys(unverified)),
        "notes": out.notes,
        "sources": {"requested": requested, "read": len(items), "failed": failed},
    }
    documents.save_version(
        session,
        workspace_id=project.workspace_id,
        project_id=project.id,
        kind="research",
        content=content,
        author_type="agent",
        author="scout",
        provider=engine.label,
        note=step.feedback,
    )
    supported = sum(1 for a in angles if a["source_urls"])
    summary = f"{plural(len(angles), 'ángulo')} ({supported} con fuentes), {plural(len(findings), 'hallazgo')} con enlace"
    if failed:
        summary += f", {plural(len(failed), 'fuente')} sin leer"
    return Applied(
        summary,
        {
            "angles": len(angles),
            "findings": len(findings),
            "sources_read": len(items),
            "sources_failed": len(failed),
        },
    )


# --- Writer: script ----------------------------------------------------------


def _prepare_script(
    session: Session, project: Project, step: Step, workspace: Workspace
) -> Prepared:
    text, data = _base(session, project, step, workspace)
    research = documents.current_content(session, project.id, "research")
    research_step = session.scalar(
        select(Step).where(Step.project_id == project.id, Step.kind == "research")
    )
    chosen = (research_step.output or {}).get("chosen_angle") if research_step else None
    angle = (
        chosen or (research.get("angles") or [{}])[0].get("title", "") or project.title
    )
    findings = research.get("findings", [])
    data["angle"] = angle
    evidence = "\n".join(
        f"- {f['claim']} — {f['source_title']} ({f['url']}), confidence {f['confidence']}"
        for f in findings[:12]
    )
    previous = documents.current_content(session, project.id, "script")
    revision = ""
    if step.feedback and previous:
        revision = (
            "\n\nYOUR PREVIOUS VERSION (revise it, keep what was not criticised):\n"
            + _script_text(previous)[:6000]
        )
    description = f"""{text}

APPROVED ANGLE: {angle}

VERIFIED FINDINGS YOU MAY USE (nothing else may be stated as fact):
{evidence or "- None. Do not state facts or figures; write from the brief only and leave [brackets] where the user must add their own facts."}

SCRIPT STRUCTURE TO FOLLOW: {" → ".join(data["structure"]) or "hook → development → close"}

PLATFORM LIMITS
{_limits_text(project.platforms)}{revision}

YOUR TASK
Write the script and all texts in the brand voice: three alternative hooks, an outline, the script section by section (what is
said, plus on-screen text or shot suggestions), a call to action, three title options, and copy for each target platform (title,
description, hashtags). Then call check_platform_copy with your platform copy and fix whatever it reports before answering."""
    return Prepared(
        description,
        "A JSON object with working_title, hooks, outline, sections, cta, titles, platform_copy and notes.",
        data,
    )


def _script_text(content: dict[str, Any]) -> str:
    lines = [
        f"Title: {content.get('working_title', '')}",
        "Hooks: " + " | ".join(content.get("hooks", [])),
    ]
    for section in content.get("sections", []):
        lines.append(f"[{section.get('heading', '')}] {section.get('text', '')}")
    lines.append(f"CTA: {content.get('cta', '')}")
    return "\n".join(lines)


def copy_checks(platform_copy: list[dict[str, Any]]) -> list[dict[str, Any]]:
    report = []
    for item in platform_copy:
        platform = item.get("platform", "")
        if platform not in specs.SPECS:
            continue
        title, description = platform_fields(
            platform,
            item.get("title", ""),
            item.get("description", ""),
            item.get("hashtags", []),
        )
        checks = specs.validate_metadata(platform, title, description, [])
        report.append(
            {"platform": platform, "status": specs.overall(checks), "checks": checks}
        )
    return report


_PLACEHOLDER = re.compile(r"\[[^\]\n]{2,}\]")


def count_placeholders(content: dict[str, Any]) -> int:
    """Bracketed gaps left wherever a person would find them when editing the script.

    That is what is said, what is shown and the texts for each platform. The
    agent's notes are commentary (they explain the convention with the word
    "[corchetes]") and are not counted.
    """
    texts = [content.get("working_title", ""), content.get("cta", "")]
    texts += [
        *content.get("hooks", []),
        *content.get("outline", []),
        *content.get("titles", []),
    ]
    for section in content.get("sections", []):
        texts += [
            section.get("heading", ""),
            section.get("text", ""),
            section.get("visual", ""),
        ]
    for copy in content.get("platform_copy", []):
        texts += [copy.get("title", ""), copy.get("description", "")]
    return sum(len(_PLACEHOLDER.findall(text or "")) for text in texts)


def _apply_script(
    session: Session,
    ctx: RunContext,
    out: schemas.ScriptOutput,
    engine: EngineInfo,
    project: Project,
    step: Step,
) -> Applied:
    content = out.model_dump()
    content["platform_copy"] = [
        c for c in content["platform_copy"] if c["platform"] in project.platforms
    ]
    missing = [
        p
        for p in project.platforms
        if p not in {c["platform"] for c in content["platform_copy"]}
    ]
    for platform in missing:
        content["platform_copy"].append(
            {"platform": platform, "title": "", "description": "", "hashtags": []}
        )
    if not content["sections"] and not content["hooks"]:
        raise StepFailedError("Writer no devolvió ni guion ni ganchos.")
    content["copy_checks"] = copy_checks(content["platform_copy"])
    content["placeholders"] = count_placeholders(content)
    documents.save_version(
        session,
        workspace_id=project.workspace_id,
        project_id=project.id,
        kind="script",
        content=content,
        author_type="agent",
        author="writer",
        provider=engine.label,
        note=step.feedback,
    )
    return Applied(
        script_summary(content),
        {"sections": len(content["sections"]), "placeholders": content["placeholders"]},
    )


def script_summary(content: dict[str, Any]) -> str:
    """One line about the script as it stands; also used when a person edits it, so the step never describes an old version."""
    summary = (
        f"Guion de {plural(len(content['sections']), 'sección', 'secciones')}, {plural(len(content['hooks']), 'gancho')} "
        f"y textos para {plural(len(content['platform_copy']), 'plataforma')}"
    )
    if content["placeholders"]:
        summary += f" · {plural(content['placeholders'], 'marcador', 'marcadores')} por completar"
    return summary


# --- Clipper: clips ----------------------------------------------------------


def _source_video(session: Session, project: Project) -> Asset | None:
    return (
        session.get(Asset, project.source_asset_id) if project.source_asset_id else None
    )


def _precheck_clips(session: Session, project: Project) -> Blocked | None:
    asset = _source_video(session, project)
    if asset is None:
        return Blocked(
            "missing_video",
            "Falta el vídeo del proyecto.",
            "Sube el vídeo original (o grábalo a partir del guion y súbelo) para que Clipper pueda proponer clips.",
        )
    if not asset.rights_declared:
        return Blocked(
            "missing_rights",
            "Falta declarar que tienes derechos de uso sobre el vídeo.",
            "Confirma los derechos del vídeo en el proyecto.",
        )
    if ffmpeg.locate() is None:
        return FFMPEG_MISSING
    return None


def _prepare_clips(
    session: Session, project: Project, step: Step, workspace: Workspace
) -> Prepared:
    text, data = _base(session, project, step, workspace)
    options = project.options or {}
    description = f"""{text}

YOUR TASK
1. Call the analyze_video tool once. It measures the uploaded video and returns ranked clip candidates with exact timestamps,
   the signals behind each one and the transcript excerpt when a transcript exists.
2. Pick the {options.get("clip_count", 5)} candidates that best serve the audience and goal. Refer to each by its candidate_id.
3. Give every pick a short working title and the reason it can work.

RULES
- Only pick candidate_ids returned by the tool. Never invent timestamps.
- Base each reason on the measured facts and the transcript excerpt. If there is no transcript, say that the choice rests on
  audio and picture signals only; do not describe what is said or shown."""
    return Prepared(
        description,
        "A JSON object with picks (candidate_id, title, reason) and notes.",
        data,
    )


def _apply_clips(
    session: Session,
    ctx: RunContext,
    out: schemas.ClipsOutput,
    engine: EngineInfo,
    project: Project,
    step: Step,
) -> Applied:
    analysis = ctx.tool_results.get("analyze_video")
    if not isinstance(analysis, dict) or analysis.get("error"):
        raise StepFailedError(
            (analysis or {}).get("error") or "Clipper no analizó el vídeo."
        )
    candidates = {c["id"]: c for c in analysis["candidates"]}
    picks = [
        (p, candidates[p.candidate_id])
        for p in out.picks
        if p.candidate_id in candidates
    ]
    if not picks:
        raise StepFailedError(
            "Clipper no eligió ningún tramo de los que devolvió el análisis."
        )
    # A new proposal replaces the previous one, except for what a person already built on:
    # clips they marked by hand and clips that have exported pieces.
    existing = list(session.scalars(select(Clip).where(Clip.project_id == project.id)))
    with_pieces = set(
        session.scalars(select(Render.clip_id).where(Render.project_id == project.id))
    )
    kept = [c for c in existing if c.origin == "user" or c.id in with_pieces]
    for clip in existing:
        if clip not in kept:
            session.delete(clip)
    taken = {(round(c.start, 1), round(c.end, 1)) for c in kept}
    position = len(kept)
    picked_ids = set()
    for pick, candidate in picks:
        picked_ids.add(candidate["id"])
        if (round(candidate["start"], 1), round(candidate["end"], 1)) in taken:
            continue
        session.add(
            Clip(
                workspace_id=project.workspace_id,
                project_id=project.id,
                asset_id=analysis["asset_id"],
                start=candidate["start"],
                end=candidate["end"],
                title=pick.title.strip()[:200],
                reason=pick.reason.strip(),
                excerpt=candidate.get("excerpt", ""),
                signals={
                    **candidate["signals"],
                    "picked": True,
                    "facts": candidate.get("facts", ""),
                },
                score=candidate["score"],
                position=position,
                status="selected",  # recommended clips start ticked; the person unticks what they do not want
            )
        )
        position += 1
    alternatives = 0
    for candidate in analysis["candidates"]:
        if (
            candidate["id"] in picked_ids
            or (round(candidate["start"], 1), round(candidate["end"], 1)) in taken
        ):
            continue
        alternatives += 1
        session.add(
            Clip(
                workspace_id=project.workspace_id,
                project_id=project.id,
                asset_id=analysis["asset_id"],
                start=candidate["start"],
                end=candidate["end"],
                title=f"Alternativa {alternatives}",
                reason=candidate.get("facts", ""),
                excerpt=candidate.get("excerpt", ""),
                signals={
                    **candidate["signals"],
                    "picked": False,
                    "facts": candidate.get("facts", ""),
                },
                score=candidate["score"],
                position=position,
            )
        )
        position += 1
    summary = f"{plural(len(picks), 'clip recomendado', 'clips recomendados')} y {plural(alternatives, 'alternativa')}"
    if not analysis["transcript"]["available"]:
        summary += " · sin transcripción (solo señales de audio e imagen)"
    return Applied(
        summary,
        {
            "picked": len(picks),
            "alternatives": alternatives,
            "method": analysis["method"],
            "transcript": analysis["transcript"],
            "signals": analysis["signals"],
            "notes": out.notes,
        },
    )


# --- Editor: edit ------------------------------------------------------------


def _selected_clips(session: Session, project: Project) -> list[Clip]:
    return list(
        session.scalars(
            select(Clip)
            .where(Clip.project_id == project.id, Clip.status == "selected")
            .order_by(Clip.position)
        )
    )


def _precheck_edit(session: Session, project: Project) -> Blocked | None:
    clips = [c for c in _selected_clips(session, project)]
    if not clips:
        return Blocked(
            "no_clips",
            "No hay ningún clip seleccionado.",
            "Selecciona al menos un clip en el paso de Clipper.",
        )
    if ffmpeg.locate() is None:
        return FFMPEG_MISSING
    return None


def _prepare_edit(
    session: Session, project: Project, step: Step, workspace: Workspace
) -> Prepared:
    text, data = _base(session, project, step, workspace)
    clips = _selected_clips(session, project)
    uploaded = {
        r.clip_id
        for r in session.scalars(
            select(Render).where(
                Render.project_id == project.id, Render.origin == "upload"
            )
        )
    }
    clips = [c for c in clips if c.id not in uploaded]
    source = _source_video(session, project)
    media = production.media_info(session, source) if source else {}
    video = media.get("video") or {}
    formats = production.formats_for(project.platforms)
    transcript = bool(production.transcript_of(source)) if source else False
    data.update(
        {
            "clips": [
                {"id": c.id, "title": c.title, "start": c.start, "end": c.end}
                for c in clips
            ],
            "formats": formats,
            "source_width": video.get("width", 0),
            "source_height": video.get("height", 0),
            "transcript_available": transcript,
        }
    )
    listing = "\n".join(
        f"- clip_id {c.id}: «{c.title}», {c.end - c.start:.1f} s" for c in clips
    )
    description = f"""{text}

APPROVED CLIPS
{listing}

SOURCE VIDEO: {video.get("width")}x{video.get("height")} px. Transcript available for subtitles: {"yes" if transcript else "no"}.
FORMATS NEEDED for the target platforms: {", ".join(formats)}.
Brand default for burned-in subtitles: {"on" if data["subtitles_default"] else "off"}.

YOUR TASK
Call render_pieces once with one entry for every approved clip in every needed format. Choose the framing for each piece: use
"crop" only when the source already has the target shape; otherwise prefer "blur" so nothing is cut out of the picture. Ask for
subtitles only when a transcript is available. Then summarise the measured result of the export checks."""
    return Prepared(
        description, "A JSON object with summary and notes.", data, max_iter=5
    )


def _apply_edit(
    session: Session,
    ctx: RunContext,
    out: schemas.EditOutput,
    engine: EngineInfo,
    project: Project,
    step: Step,
) -> Applied:
    result = ctx.tool_results.get("render_pieces")
    if not isinstance(result, dict) or result.get("error"):
        raise StepFailedError(
            (result or {}).get("error") or "Editor no exportó ninguna pieza."
        )
    rendered = result.get("rendered", [])
    ready = [r for r in rendered if r.get("status") == "ready"]
    errors = [r for r in rendered if r.get("status") != "ready"]
    if not ready:
        skipped = result.get("skipped") or []
        raise StepFailedError(
            errors[0].get("error", "La exportación falló.")
            if errors
            else (skipped[0] if skipped else "Editor no pidió exportar ninguna pieza.")
        )
    warnings = sum(
        1 for r in ready for c in r.get("checks", []) if c["status"] in ("warn", "fail")
    )
    summary = plural(len(ready), "pieza exportada", "piezas exportadas")
    if errors:
        summary += f", {len(errors)} con error"
    if warnings:
        summary += f" · {plural(warnings, 'aviso')} en la lista de comprobación"
    return Applied(
        summary,
        {
            "ready": len(ready),
            "errors": len(errors),
            "warnings": warnings,
            "skipped": result.get("skipped", []),
            "notes": out.notes,
        },
    )


# --- Designer: design --------------------------------------------------------


def design_kinds(platforms: list[str]) -> list[str]:
    kinds: list[str] = []
    for platform in platforms:
        kind = specs.SPECS[platform].cover
        if kind not in kinds:
            kinds.append(kind)
    return kinds


def _prepare_design(
    session: Session, project: Project, step: Step, workspace: Workspace
) -> Prepared:
    text, data = _base(session, project, step, workspace)
    script = documents.current_content(session, project.id, "script")
    kinds = design_kinds(project.platforms)
    source = _source_video(session, project)
    has_video = (
        bool(source and (source.meta or {}).get("media", {}).get("video"))
        and ffmpeg.locate() is not None
    )
    data.update(
        {
            "design_kinds": kinds,
            "headline_source": project.title,
            "has_video": has_video,
        }
    )
    titles = " | ".join(script.get("titles", [])) or project.title
    hooks = " | ".join(script.get("hooks", []))
    description = f"""{text}

APPROVED SCRIPT TITLES: {titles}
HOOKS: {hooks}

DESIGNS NEEDED: {", ".join(kinds)} (thumbnail = 1280x720 for YouTube, cover = 1080x1920 for vertical platforms).
A frame of the uploaded video is {"available" if has_video else "not available"} as background.

YOUR TASK
Call compose_designs once with one entry per design needed. Write a headline of at most five words that is readable on a phone,
pick a layout (side, band or center) and a background ({"frame or gradient" if has_video else "gradient"}). The tool applies the
brand colours, typography and logo and measures contrast, text size and the platform safe zone. If a check fails, call the tool
again with a shorter headline. Then summarise the measured checks."""
    return Prepared(
        description, "A JSON object with summary and notes.", data, max_iter=6
    )


def _apply_design(
    session: Session,
    ctx: RunContext,
    out: schemas.DesignOutput,
    engine: EngineInfo,
    project: Project,
    step: Step,
) -> Applied:
    result = ctx.tool_results.get("compose_designs")
    if not isinstance(result, dict) or result.get("error"):
        raise StepFailedError(
            (result or {}).get("error") or "Designer no compuso ningún diseño."
        )
    designs = result.get("designs", [])
    ready = [d for d in designs if d.get("status") == "ready"]
    if not ready:
        skipped = result.get("skipped") or []
        raise StepFailedError(
            designs[0].get("error", "No se pudo componer el diseño.")
            if designs
            else (skipped[0] if skipped else "Designer no pidió ningún diseño.")
        )
    issues = sum(
        1 for d in ready for c in d.get("checks", []) if c["status"] in ("warn", "fail")
    )
    summary = f"{plural(len(ready), 'diseño compuesto', 'diseños compuestos')} con el kit de marca"
    if issues:
        summary += f" · {plural(issues, 'aviso')}"
    return Applied(summary, {"ready": len(ready), "issues": issues, "notes": out.notes})


# --- Publisher: publish ------------------------------------------------------


def _precheck_publish(session: Session, project: Project) -> Blocked | None:
    ready = session.scalar(
        select(Render.id)
        .where(Render.project_id == project.id, Render.status == "ready")
        .limit(1)
    )
    if ready is None:
        return Blocked(
            "no_pieces",
            "No hay ninguna pieza de vídeo lista para publicar.",
            "Exporta al menos una pieza con Editor o sube una pieza terminada desde el estudio.",
        )
    return None


def _prepare_publish(
    session: Session, project: Project, step: Step, workspace: Workspace
) -> Prepared:
    text, data = _base(session, project, step, workspace)
    publishing.ensure_publications(session, project)
    script = documents.current_content(session, project.id, "script")
    drafts = list(
        session.scalars(
            select(Publication)
            .where(
                Publication.project_id == project.id,
                Publication.status == "draft",
                Publication.approved_at.is_(None),
            )
            .order_by(Publication.platform, Publication.created_at)
        )
    )
    rows = []
    listing = []
    for pub in drafts:
        render = session.get(Render, pub.render_id) if pub.render_id else None
        clip = session.get(Clip, render.clip_id) if render else None
        asset = (
            session.get(Asset, render.asset_id) if render and render.asset_id else None
        )
        seconds = (
            ((asset.meta or {}).get("media") or {}).get("duration") if asset else None
        )
        rows.append(
            {
                "id": pub.id,
                "platform": pub.platform,
                "clip_title": clip.title if clip else "",
            }
        )
        listing.append(
            f"- publication_id {pub.id}: {PLATFORMS[pub.platform]['name']}, clip «{clip.title if clip else ''}», {seconds or '?'} s"
            + (f", says: «{clip.excerpt[:200]}»" if clip and clip.excerpt else "")
        )
    data.update({"drafts": rows, "platform_copy": script.get("platform_copy", [])})
    copy_lines = "\n".join(
        f"- {c['platform']}: title «{c.get('title', '')}» | description «{c.get('description', '')[:300]}» | hashtags {' '.join(c.get('hashtags', []))}"
        for c in script.get("platform_copy", [])
    )
    description = f"""{text}

APPROVED PLATFORM COPY FROM THE SCRIPT
{copy_lines or "- none"}

DRAFTS TO PREPARE
{chr(10).join(listing) or "- none"}

PLATFORM LIMITS
{_limits_text(project.platforms)}

YOUR TASK
Call save_drafts once with title, description and tags for every draft, adapted to its platform and to what that clip is about.
The tool validates each draft against the platform requirements; if it reports problems, fix them and call it again.
You only prepare drafts. Publishing is done later by a person through the official connectors."""
    return Prepared(
        description, "A JSON object with summary and notes.", data, max_iter=6
    )


def _apply_publish(
    session: Session,
    ctx: RunContext,
    out: schemas.PublishOutput,
    engine: EngineInfo,
    project: Project,
    step: Step,
) -> Applied:
    result = ctx.tool_results.get("save_drafts")
    if not isinstance(result, dict) or result.get("error"):
        raise StepFailedError(
            (result or {}).get("error") or "Publisher no guardó ningún borrador."
        )
    drafts = list(
        session.scalars(
            select(Publication).where(
                Publication.project_id == project.id, Publication.status == "draft"
            )
        )
    )
    if not drafts:
        raise StepFailedError("No hay borradores de publicación.")
    for pub in drafts:
        publishing.validate(session, pub)
    ready = sum(
        1 for p in drafts if not any(c["status"] == "fail" for c in p.validation)
    )
    summary = (
        f"{plural(len(drafts), 'borrador preparado', 'borradores preparados')}: {ready} "
        f"{choose(ready, 'cumple', 'cumplen')} los requisitos, {len(drafts) - ready} con requisitos pendientes. Nada publicado"
    )
    return Applied(summary, {"drafts": len(drafts), "ready": ready, "notes": out.notes})


# --- Community ---------------------------------------------------------------


def _precheck_community(session: Session, project: Project) -> Blocked | None:
    pending = session.scalar(
        select(Comment.id)
        .where(Comment.project_id == project.id, Comment.category == "")
        .limit(1)
    )
    if pending is None:
        return Blocked(
            "no_comments",
            "No hay comentarios nuevos que clasificar.",
            "Sincroniza una cuenta conectada o importa comentarios a mano.",
        )
    return None


def _prepare_community(
    session: Session, project: Project, step: Step, workspace: Workspace
) -> Prepared:
    text, data = _base(session, project, step, workspace)
    description = f"""{text}

YOUR TASK
1. Call load_comments once to get the comments that have not been triaged.
2. Classify each one as question, positive, spam, issue or other, with a one-line reason. The tool shows what a keyword rule
   would say; correct it when the text means something else.
3. Draft a reply in the brand voice for questions, positive comments and issues. Leave the reply empty for spam.

RULES
- Replies are suggestions for a person to approve; never promise anything the brief does not support.
- If answering needs information you do not have, write the reply with a [bracketed] gap for the person to fill."""
    return Prepared(
        description,
        "A JSON object with items (comment_id, category, reason, reply) and notes.",
        data,
    )


def _apply_community(
    session: Session,
    ctx: RunContext,
    out: schemas.CommunityOutput,
    engine: EngineInfo,
    project: Project,
    step: Step,
) -> Applied:
    loaded = ctx.tool_results.get("load_comments")
    if not isinstance(loaded, dict) or loaded.get("error"):
        raise StepFailedError(
            (loaded or {}).get("error") or "Community no leyó los comentarios."
        )
    known = {c["comment_id"]: c for c in loaded.get("comments", [])}
    counts: dict[str, int] = {}
    replies = 0
    seen = set()
    for item in out.items:
        if item.comment_id not in known or item.comment_id in seen:
            continue
        seen.add(item.comment_id)
        row = session.get(Comment, item.comment_id)
        if row is None:
            continue
        row.category = item.category
        row.category_by = "rules" if engine.is_demo else "llm"
        row.category_reason = (
            item.reason.strip() or known[item.comment_id]["rule_reason"]
        )
        reply = item.reply.strip() if item.category != "spam" else ""
        if reply and row.reply_status in ("none", "suggested"):
            row.reply_text, row.reply_status, row.reply_by = (
                reply,
                "suggested",
                "community",
            )
            replies += 1
        counts[item.category] = counts.get(item.category, 0) + 1
    # Anything the model skipped still gets the transparent rule, so nothing is left unlabelled.
    for comment_id, comment in known.items():
        if comment_id in seen:
            continue
        row = session.get(Comment, comment_id)
        if row is not None:
            row.category, row.category_by, row.category_reason = (
                comment["rule_category"],
                "rules",
                comment["rule_reason"],
            )
            counts[row.category] = counts.get(row.category, 0) + 1
    total = sum(counts.values())
    if total == 0:
        raise StepFailedError("No había comentarios que clasificar.")
    labels = {
        "question": ("pregunta", "preguntas"),
        "positive": ("positivo", "positivos"),
        "issue": ("incidencia", "incidencias"),
        "spam": ("de spam", "de spam"),
        "other": ("de otro tipo", "de otro tipo"),
    }
    parts = ", ".join(
        plural(counts[key], *labels[key]) for key in labels if counts.get(key)
    )
    suggested = (
        f"{plural(replies, 'respuesta sugerida', 'respuestas sugeridas')} a la espera de tu aprobación"
        if replies
        else "ninguna respuesta sugerida"
    )
    summary = f"{plural(total, 'comentario clasificado', 'comentarios clasificados')} ({parts}) y {suggested}"
    return Applied(summary, {"counts": counts, "replies": replies, "notes": out.notes})


# --- Analyst -----------------------------------------------------------------


def _precheck_analysis(session: Session, project: Project) -> Blocked | None:
    has_metric = session.scalar(
        select(Metric.id)
        .join(Publication, Publication.id == Metric.publication_id)
        .where(Publication.project_id == project.id)
        .limit(1)
    )
    if has_metric is None:
        return Blocked(
            "no_metrics",
            "Aún no hay métricas de este proyecto.",
            "Conecta una cuenta para leerlas o añade las cifras a mano en Analítica.",
        )
    return None


def _prepare_analysis(
    session: Session, project: Project, step: Step, workspace: Workspace
) -> Prepared:
    text, data = _base(session, project, step, workspace)
    description = f"""{text}

YOUR TASK
1. Call compute_stats once. It returns the metrics that exist for published pieces across the workspace, their origin
   (platform or typed in by hand), and comparisons that are marked sufficient only when the groups are large enough.
2. Write a short summary and up to five actionable recommendations for future content.
3. Under data_gaps, list what cannot be concluded and why.

RULES
- Every recommendation must cite, in evidence, the exact figures from the tool that support it.
- Do not compare groups the tool marks as insufficient. Do not invent benchmarks or industry averages.
- Say when a figure was typed in by hand rather than read from the platform."""
    return Prepared(
        description,
        "A JSON object with summary, recommendations (text, evidence) and data_gaps.",
        data,
    )


_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")


def _readings(raw: str) -> list[float]:
    """Every value a written figure can stand for: «18.400» is eighteen thousand in Spanish and 18.4 in English."""
    marks = [ch for ch in raw if ch in ".,"]
    if not marks:
        return [float(raw)]
    readings: list[float] = []
    last = max(raw.rfind("."), raw.rfind(","))
    head, tail = raw[:last], raw[last + 1 :]
    if (
        raw[last] not in head
    ):  # the last mark is the decimal one unless the same mark already grouped thousands
        readings.append(float(head.replace(".", "").replace(",", "") + "." + tail))
    parts = re.split(r"[.,]", raw)
    if (
        len(set(marks)) == 1
        and 1 <= len(parts[0]) <= 3
        and all(len(part) == 3 for part in parts[1:])
    ):
        readings.append(float("".join(parts)))
    return readings


def _numbers_in(node: Any, out: set[float]) -> None:
    if isinstance(node, bool):
        return
    if isinstance(node, (int, float)):
        out.add(round(float(node), 2))
    elif isinstance(node, dict):
        for value in node.values():
            _numbers_in(value, out)
    elif isinstance(node, list):
        for value in node:
            _numbers_in(value, out)


def unsupported_numbers(texts: list[str], stats: dict[str, Any]) -> list[str]:
    """Figures quoted as evidence that do not appear in the computed statistics."""
    known: set[float] = set()
    _numbers_in(stats, known)
    missing = []
    for text in texts:
        cleaned = text.replace("\u202f", "").replace("\u00a0", "")
        for raw in _NUMBER.findall(cleaned):
            readings = _readings(raw)
            if (
                len(readings) == 1
                and readings[0] < 10
                and "." not in raw
                and "," not in raw
            ):
                continue  # small counts ("3 piezas") are not worth flagging
            if not any(
                abs(value - k) <= max(0.06, abs(k) * 0.005)
                for value in readings
                for k in known
            ):
                missing.append(raw)
    return missing


def _apply_analysis(
    session: Session,
    ctx: RunContext,
    out: schemas.AnalysisOutput,
    engine: EngineInfo,
    project: Project,
    step: Step,
) -> Applied:
    stats = ctx.tool_results.get("compute_stats")
    if not isinstance(stats, dict) or stats.get("error"):
        raise StepFailedError(
            (stats or {}).get("error") or "Analyst no calculó las estadísticas."
        )
    for old in session.scalars(
        select(Recommendation).where(
            Recommendation.workspace_id == project.workspace_id,
            Recommendation.status == "new",
        )
    ):
        session.delete(old)
    saved = 0
    for item in out.recommendations[:8]:
        if not item.text.strip():
            continue
        evidence = [{"text": e} for e in item.evidence if e.strip()]
        flagged = unsupported_numbers([item.text, *item.evidence], stats)
        session.add(
            Recommendation(
                workspace_id=project.workspace_id,
                project_id=project.id,
                text=item.text.strip(),
                evidence=evidence
                + (
                    [
                        {
                            "text": "Contiene cifras que no figuran en las estadísticas calculadas: "
                            + ", ".join(flagged),
                            "flag": True,
                        }
                    ]
                    if flagged
                    else []
                ),
                basis="rules" if engine.is_demo else "llm",
            )
        )
        saved += 1
    summary = f"{plural(stats['with_metrics'], 'pieza analizada', 'piezas analizadas')} · {plural(saved, 'recomendación', 'recomendaciones')}"
    if out.data_gaps:
        summary += f" · {plural(len(out.data_gaps), 'límite', 'límites')} de los datos"
    return Applied(
        summary,
        {
            "summary": out.summary,
            "data_gaps": [g for g in out.data_gaps if g.strip()],
            "recommendations": saved,
            "with_metrics": stats["with_metrics"],
            "sources": stats["sources"],
        },
    )


SPECS: dict[str, StepSpec] = {
    "research": StepSpec(
        "research",
        schemas.ResearchOutput,
        _no_block,
        _prepare_research,
        _apply_research,
    ),
    "script": StepSpec(
        "script", schemas.ScriptOutput, _no_block, _prepare_script, _apply_script
    ),
    "clips": StepSpec(
        "clips", schemas.ClipsOutput, _precheck_clips, _prepare_clips, _apply_clips
    ),
    "edit": StepSpec(
        "edit", schemas.EditOutput, _precheck_edit, _prepare_edit, _apply_edit
    ),
    "design": StepSpec(
        "design", schemas.DesignOutput, _no_block, _prepare_design, _apply_design
    ),
    "publish": StepSpec(
        "publish",
        schemas.PublishOutput,
        _precheck_publish,
        _prepare_publish,
        _apply_publish,
    ),
    "community": StepSpec(
        "community",
        schemas.CommunityOutput,
        _precheck_community,
        _prepare_community,
        _apply_community,
    ),
    "analysis": StepSpec(
        "analysis",
        schemas.AnalysisOutput,
        _precheck_analysis,
        _prepare_analysis,
        _apply_analysis,
    ),
}

__all__ = [
    "SPECS",
    "Applied",
    "Blocked",
    "Prepared",
    "StepFailedError",
    "StepSpec",
    "copy_checks",
    "count_placeholders",
    "design_kinds",
]
