"""Pre-flight review: rule checks run by the orchestrator before publishing.

No model is involved. Every line of the report is computed from stored state:
exported files, their measured properties, the approved texts and the rights
the user declared.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from creator_office.agents.steps import copy_checks, count_placeholders, design_kinds
from creator_office.connectors import platform_specs as specs
from creator_office.models import Asset, Clip, Design, Project, Render, Step
from creator_office.pipeline.catalog import PLATFORMS
from creator_office.services import documents
from creator_office.text import plural

FORMAT_NAMES = {
    "vertical": "vertical (9:16)",
    "horizontal": "horizontal (16:9)",
    "square": "cuadrado (1:1)",
}


def _item(
    area: str, status: str, label: str, detail: str, action: str = ""
) -> dict[str, Any]:
    return {
        "area": area,
        "status": status,
        "label": label,
        "detail": detail,
        "action": action,
    }


def review(session: Session, project: Project) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    steps = {
        s.kind: s
        for s in session.scalars(select(Step).where(Step.project_id == project.id))
    }
    renders = list(
        session.scalars(select(Render).where(Render.project_id == project.id))
    )
    ready = [r for r in renders if r.status == "ready"]

    # Formato
    for platform in project.platforms:
        fmt = PLATFORMS[platform]["format"]
        pieces = [r for r in ready if r.format == fmt]
        name = PLATFORMS[platform]["name"]
        if not pieces:
            checks.append(
                _item(
                    "Formato",
                    "fail",
                    name,
                    f"No hay ninguna pieza en formato {FORMAT_NAMES[fmt]}.",
                    "Exporta o sube una pieza en ese formato.",
                )
            )
            continue
        problems = []
        for render in pieces:
            for check in render.checks or []:
                if check["status"] == "fail" and check["id"] in (
                    "resolution",
                    f"{platform}_duration",
                ):
                    problems.append(check["detail"])
        if problems:
            checks.append(
                _item(
                    "Formato",
                    "fail",
                    name,
                    f"{plural(len(problems), 'problema')}: {problems[0]}",
                    "Ajusta el clip y vuelve a exportarlo.",
                )
            )
        else:
            checks.append(
                _item(
                    "Formato",
                    "pass",
                    name,
                    f"{plural(len(pieces), 'pieza')} en formato {FORMAT_NAMES[fmt]} dentro de los límites de duración.",
                )
            )

    # Subtítulos
    if ready:
        burned = [r for r in ready if (r.settings or {}).get("subtitles_burned")]
        uploaded = [r for r in ready if r.origin == "upload"]
        if len(burned) == len(ready) - len(uploaded) and burned:
            checks.append(
                _item(
                    "Subtítulos",
                    "pass",
                    "Subtítulos incrustados",
                    f"{plural(len(burned), 'pieza exportada', 'piezas exportadas')} con subtítulos dentro de la zona segura.",
                )
            )
        elif burned:
            checks.append(
                _item(
                    "Subtítulos",
                    "warn",
                    "Subtítulos incrustados",
                    f"{len(burned)} de {len(ready) - len(uploaded)} piezas exportadas llevan subtítulos.",
                    "Activa los subtítulos en las piezas que falten.",
                )
            )
        elif len(uploaded) < len(ready):
            checks.append(
                _item(
                    "Subtítulos",
                    "warn",
                    "Sin subtítulos",
                    "Ninguna pieza exportada lleva subtítulos incrustados.",
                    "Sube un archivo .srt/.vtt del vídeo (o configura la transcripción) y vuelve a exportar con subtítulos.",
                )
            )
        if uploaded:
            checks.append(
                _item(
                    "Subtítulos",
                    "info",
                    "Piezas subidas por ti",
                    f"{plural(len(uploaded), 'pieza subida', 'piezas subidas')} ya con edición propia: sus subtítulos no se pueden comprobar.",
                )
            )

    # Textos
    script = documents.current_content(session, project.id, "script")
    script_step = steps.get("script")
    if not script or script_step is None or script_step.status != "completed":
        checks.append(
            _item(
                "Textos",
                "warn",
                "Guion",
                "No hay un guion aprobado para este proyecto.",
                "Aprueba el guion o confirma que no lo necesitas.",
            )
        )
    else:
        placeholders = count_placeholders(script)
        if placeholders:
            checks.append(
                _item(
                    "Textos",
                    "warn",
                    "Guion",
                    f"El guion aprobado conserva {plural(placeholders, 'marcador', 'marcadores')} entre corchetes sin completar.",
                    "Complétalos en el guion si vas a grabar o a publicar con él.",
                )
            )
        else:
            checks.append(
                _item(
                    "Textos",
                    "pass",
                    "Guion",
                    "Guion aprobado y sin marcadores pendientes.",
                )
            )

    # Metadatos
    copy = {c["platform"]: c for c in script.get("platform_copy", [])} if script else {}
    reports = {r["platform"]: r for r in copy_checks(list(copy.values()))}
    for platform in project.platforms:
        name = PLATFORMS[platform]["name"]
        report = reports.get(platform)
        if report is None:
            checks.append(
                _item(
                    "Metadatos",
                    "warn",
                    name,
                    "No hay textos preparados para esta plataforma.",
                    "Publisher los pedirá vacíos: complétalos en cada borrador.",
                )
            )
            continue
        failing = [c for c in report["checks"] if c["status"] == "fail"]
        warning = [c for c in report["checks"] if c["status"] == "warn"]
        if failing:
            checks.append(
                _item(
                    "Metadatos",
                    "fail",
                    name,
                    f"{failing[0]['label']}: {failing[0]['detail']}",
                    "Corrige los textos de esta plataforma en el guion.",
                )
            )
        elif warning:
            checks.append(
                _item(
                    "Metadatos",
                    "warn",
                    name,
                    f"{warning[0]['label']}: {warning[0]['detail']}",
                )
            )
        else:
            checks.append(
                _item(
                    "Metadatos",
                    "pass",
                    name,
                    "Título, descripción y hashtags dentro de los límites.",
                )
            )

    # Derechos
    source = (
        session.get(Asset, project.source_asset_id) if project.source_asset_id else None
    )
    clip_assets = {
        c.asset_id
        for c in session.scalars(
            select(Clip).where(Clip.project_id == project.id, Clip.status == "selected")
        )
    }
    undeclared = [
        a
        for a in (session.get(Asset, asset_id) for asset_id in clip_assets)
        if a is not None and not a.rights_declared
    ]
    if source is not None and not source.rights_declared:
        undeclared.append(source)
    if undeclared:
        checks.append(
            _item(
                "Derechos",
                "fail",
                "Derechos del vídeo",
                f"{plural(len({a.id for a in undeclared}), 'archivo')} sin declaración de derechos.",
                "Declara los derechos de uso de cada vídeo.",
            )
        )
    elif clip_assets or source is not None:
        checks.append(
            _item(
                "Derechos",
                "pass",
                "Derechos del vídeo",
                "Declaraste tener derechos de uso sobre todo el material de vídeo.",
            )
        )
    checks.append(
        _item(
            "Derechos",
            "info",
            "Música y recursos",
            "No se ha añadido música ni recursos externos: las piezas exportadas solo llevan el audio original.",
        )
    )

    # Portadas
    designs = {
        d.kind: d
        for d in session.scalars(select(Design).where(Design.project_id == project.id))
    }
    for kind in design_kinds(project.platforms):
        label = "Miniatura (YouTube)" if kind == "thumbnail" else "Portada vertical"
        design = designs.get(kind)
        if design is None or design.status != "ready":
            checks.append(
                _item(
                    "Portadas",
                    "warn",
                    label,
                    "No hay diseño listo: la plataforma usará un fotograma del vídeo.",
                )
            )
            continue
        failing = [c for c in design.checks or [] if c["status"] == "fail"]
        if failing:
            checks.append(
                _item(
                    "Portadas",
                    "warn",
                    label,
                    f"{failing[0]['label']}: {failing[0]['detail']}",
                    "Ajusta el titular o la composición en el estudio.",
                )
            )
        else:
            checks.append(
                _item(
                    "Portadas",
                    "pass",
                    label,
                    "Texto legible y dentro de la zona segura.",
                )
            )

    # Errores
    step_errors = [s for s in steps.values() if s.status == "error"]
    render_errors = [r for r in renders if r.status == "error"]
    if step_errors or render_errors:
        detail = []
        if step_errors:
            detail.append(f"{plural(len(step_errors), 'paso')} con error")
        if render_errors:
            detail.append(
                plural(
                    len(render_errors), "exportación fallida", "exportaciones fallidas"
                )
            )
        checks.append(
            _item(
                "Errores",
                "warn",
                "Errores pendientes",
                ", ".join(detail) + ".",
                "Reinténtalos o descártalos antes de publicar.",
            )
        )
    else:
        checks.append(
            _item(
                "Errores",
                "pass",
                "Errores pendientes",
                "Ningún paso ni exportación en error.",
            )
        )

    counts = {
        status: sum(1 for c in checks if c["status"] == status)
        for status in ("pass", "warn", "fail", "info")
    }
    return {"checks": checks, "status": specs.overall(checks), "counts": counts}
