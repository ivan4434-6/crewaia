"""Order console for the orchestrator.

This is a command interpreter, not a conversational model: it matches a small
set of orders written in plain Spanish and runs the same engine actions the
buttons do. Anything it does not recognise it says so, and it never approves
or publishes on the user's behalf.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from creator_office.models import Project, Publication, Step
from creator_office.pipeline import engine
from creator_office.pipeline.catalog import AGENTS, STEP_BY_KIND, STEPS

HELP = (
    "Soy la consola de órdenes del orquestador (un intérprete de órdenes, no un chat con IA). Puedo:\n"
    "• «estado» o «qué falta» — resumen de los proyectos y de lo que espera tu decisión.\n"
    "• «pausa [proyecto]», «reanuda [proyecto]», «cancela [proyecto]».\n"
    "• «reintenta guion» / «relanza a Clipper» — vuelve a ejecutar un paso.\n"
    "• «pausa a Editor» / «omite diseño» — sobre un paso concreto.\n"
    "• «sincroniza comentarios» / «sincroniza métricas».\n"
    "Las aprobaciones y la publicación se hacen siempre revisando el resultado en el proyecto."
)

_STEP_WORDS: dict[str, tuple[str, ...]] = {
    "research": (
        "investigacion",
        "investigar",
        "angulo",
        "angulos",
        "scout",
        "tendencias",
        "fuentes",
    ),
    "script": ("guion", "guiones", "texto", "textos", "writer", "copy"),
    "clips": ("clips", "clip", "clipper", "cortes", "momentos"),
    "edit": (
        "edicion",
        "editar",
        "exportacion",
        "exportar",
        "editor",
        "piezas",
        "render",
    ),
    "design": (
        "diseno",
        "disenos",
        "miniatura",
        "miniaturas",
        "portada",
        "portadas",
        "designer",
    ),
    "preflight": ("revision", "preflight", "comprobaciones"),
    "publish": (
        "publicacion",
        "publicaciones",
        "borradores",
        "publisher",
        "programacion",
    ),
    "community": ("comentarios", "community", "comunidad", "respuestas"),
    "analysis": ("analitica", "analisis", "metricas", "analyst", "estadisticas"),
}


# The order is the first of these words found, reading left to right: orders are
# imperative, so "reanuda Cómo usar…" resumes even though "cómo" also asks for status.
_INTENT_WORDS: dict[str, tuple[str, ...]] = {
    "help": ("ayuda", "help", "ordenes", "comandos"),
    "status": (
        "estado",
        "resumen",
        "falta",
        "faltan",
        "pendiente",
        "pendientes",
        "como",
    ),
    "approve": ("aprueba", "aprobar", "apruebo", "aprobado", "ok"),
    "publish": ("publica", "publicar", "publicalo", "sube", "subir"),
    "sync": ("sincroniza", "sincronizar"),
    "rerun": ("reintenta", "reintentar", "relanza", "relanzar", "repite", "repetir"),
    "skip": ("omite", "omitir", "salta", "saltar"),
    "pause": ("pausa", "pausar", "deten", "detener"),
    "resume": ("reanuda", "reanudar", "continua", "continuar", "sigue", "seguir"),
    "cancel": ("cancela", "cancelar"),
}
_INTENT_BY_WORD = {
    word: intent for intent, group in _INTENT_WORDS.items() for word in group
}


@dataclass
class Reply:
    text: str
    ok: bool = True
    links: list[dict[str, str]] = field(default_factory=list)
    action: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "ok": self.ok,
            "links": self.links,
            "action": self.action,
        }


def normalize(text: str) -> str:
    plain = unicodedata.normalize("NFKD", text.lower())
    plain = "".join(ch for ch in plain if not unicodedata.combining(ch))
    return " ".join(re.sub(r"[^a-z0-9ñ ]+", " ", plain).split())


def _find_step(words: list[str]) -> str | None:
    for kind, names in _STEP_WORDS.items():
        if any(word in names for word in words):
            return kind
    return None


def _find_project(
    session: Session, workspace_id: str, raw: str, plain: str, context_id: str | None
) -> tuple[Project | None, list[Project]]:
    projects = list(
        session.scalars(
            select(Project)
            .where(Project.workspace_id == workspace_id)
            .order_by(Project.created_at.desc())
        )
    )
    quoted = re.search(r"[«\"“]([^»\"”]+)[»\"”]", raw)
    needle = normalize(quoted.group(1)) if quoted else ""
    scored: list[tuple[int, Project]] = []
    for project in projects:
        title = normalize(project.title)
        if needle and needle in title:
            scored.append((100, project))
            continue
        words = [w for w in title.split() if len(w) > 3]
        hits = sum(1 for w in words if re.search(rf"\b{re.escape(w)}\b", plain))
        if title and title in plain:
            hits += 10
        if hits:
            scored.append((hits, project))
    if scored:
        scored.sort(key=lambda pair: pair[0], reverse=True)
        best = [p for score, p in scored if score == scored[0][0]]
        if len(best) == 1:
            return best[0], projects
        return None, best
    if context_id:
        current = next((p for p in projects if p.id == context_id), None)
        if current is not None:
            return current, projects
    live = [p for p in projects if p.state != "cancelled"]
    if len(live) == 1:
        return live[0], projects
    return None, live


def _project_link(project: Project, step: str = "") -> dict[str, str]:
    href = f"/proyectos/{project.id}" + (f"?paso={step}" if step else "")
    return {"label": project.title, "href": href}


def _which(candidates: list[Project]) -> Reply:
    if not candidates:
        return Reply(
            "No hay ningún proyecto todavía. Crea uno desde «Nuevo proyecto».",
            ok=False,
            links=[{"label": "Nuevo proyecto", "href": "/proyectos/nuevo"}],
        )
    names = ", ".join(f"«{p.title}»" for p in candidates[:6])
    return Reply(
        f"¿De qué proyecto? Tengo varios: {names}. Repite la orden con el nombre.",
        ok=False,
        links=[_project_link(p) for p in candidates[:6]],
    )


def _status_line(step: Step) -> str:
    sdef = STEP_BY_KIND[step.kind]
    who = AGENTS[sdef.agent].name if sdef.agent in AGENTS else "Revisión"
    detail = {
        "awaiting_approval": f"espera tu aprobación ({step.summary})",
        "blocked": f"bloqueado: {step.blocked_reason}",
        "error": f"error: {step.error[:160]}",
        "working": "trabajando",
        "queued": "en cola",
        "paused": "en pausa",
        "completed": "completado",
        "skipped": "omitido",
        "cancelled": "cancelado",
    }.get(step.status)
    if detail is None:
        detail = (
            "a la espera de datos"
            if sdef.trigger == "data"
            else "a la espera de los pasos anteriores"
        )
    return f"• {sdef.title} ({who}): {detail}"


def _status(session: Session, workspace_id: str, project: Project | None) -> Reply:
    projects = (
        [project]
        if project
        else [
            p
            for p in session.scalars(
                select(Project)
                .where(Project.workspace_id == workspace_id)
                .order_by(Project.created_at.desc())
            )
            if p.state != "cancelled"
        ]
    )
    if not projects:
        return Reply(
            "No hay proyectos activos. Crea uno desde «Nuevo proyecto».",
            links=[{"label": "Nuevo proyecto", "href": "/proyectos/nuevo"}],
        )
    lines: list[str] = []
    links: list[dict[str, str]] = []
    for item in projects[:8]:
        steps = sorted(
            engine.steps_of(session, item.id).values(), key=lambda s: s.position
        )
        state = {"paused": " (en pausa)", "cancelled": " (cancelado)"}.get(
            item.state, ""
        )
        lines.append(f"«{item.title}»{state}")
        if project is not None:
            lines += [_status_line(s) for s in steps]
        else:
            attention = [
                s
                for s in steps
                if s.status
                in ("awaiting_approval", "blocked", "error", "working", "queued")
            ]
            lines += [_status_line(s) for s in attention] or [
                "• Nada en marcha ni pendiente de ti."
            ]
        drafts = session.scalar(
            select(Publication.id)
            .where(
                Publication.project_id == item.id,
                Publication.status == "draft",
                Publication.approved_at.is_(None),
            )
            .limit(1)
        )
        publish_step = next((s for s in steps if s.kind == "publish"), None)
        if (
            drafts is not None
            and publish_step is not None
            and publish_step.status == "completed"
        ):
            lines.append("• Hay borradores de publicación sin aprobar.")
        links.append(_project_link(item))
    return Reply("\n".join(lines), links=links, action={"type": "status"})


def _order_words(raw: str, plain: str, project: Project | None) -> list[str]:
    """The words that carry the order: the text without the project's name.

    A title is free text and may contain words that are also orders ("Cómo pausar
    un directo"), so a quoted name or a full title is set aside before reading the verb.
    """
    rest = plain
    quoted = re.search(r"[«\"“]([^»\"”]+)[»\"”]", raw)
    if quoted:
        rest = rest.replace(normalize(quoted.group(1)), " ", 1)
    if project is not None:
        title = normalize(project.title)
        if title:
            rest = re.sub(rf"\b{re.escape(title)}\b", " ", rest, count=1)
    return rest.split()


def execute(
    session: Session,
    *,
    workspace_id: str,
    user_id: str,
    user_name: str,
    can_edit: bool,
    text: str,
    project_id: str | None = None,
) -> Reply:
    plain = normalize(text)
    if not plain:
        return Reply(HELP, ok=False)
    if plain in ("que puedes hacer", "hola", "buenas"):
        return Reply(HELP, action={"type": "help"})

    project, candidates = _find_project(session, workspace_id, text, plain, project_id)
    words = _order_words(text, plain, project)
    intent = next(
        (_INTENT_BY_WORD[word] for word in words if word in _INTENT_BY_WORD), ""
    )
    step_kind = _find_step(words)

    def has(*options: str) -> bool:
        return any(option in words for option in options)

    if intent == "help":
        return Reply(HELP, action={"type": "help"})

    if intent == "status":
        named = project is not None and (bool(project_id) or _mentions(plain, project))
        return _status(session, workspace_id, project if named else None)

    if intent == "approve":
        target = project or (candidates[0] if candidates else None)
        return Reply(
            "No apruebo por ti: una aprobación significa que has revisado el resultado. Ábrelo en el proyecto y decide allí.",
            ok=False,
            links=[_project_link(target, step_kind or "")] if target else [],
        )
    if intent == "publish":
        target = project or (candidates[0] if candidates else None)
        return Reply(
            "No publico desde aquí. Publicar exige que apruebes el contenido y des la confirmación final en la pestaña «Publicar» del proyecto.",
            ok=False,
            links=[
                {"label": target.title, "href": f"/proyectos/{target.id}?tab=publicar"}
            ]
            if target
            else [],
        )

    if not intent:
        return Reply("No he entendido la orden. " + HELP, ok=False)
    if not can_edit:
        return Reply(
            "Tu rol en este espacio es de solo lectura: puedo darte el estado, pero no ejecutar órdenes.",
            ok=False,
        )

    if intent == "sync":
        target = (
            "metrics"
            if has("metricas", "analitica", "estadisticas")
            else "comments"
            if has("comentarios")
            else ""
        )
        if not target:
            return Reply("¿Qué sincronizo: «comentarios» o «métricas»?", ok=False)
        page = "/analitica" if target == "metrics" else "/comunidad"
        return Reply(
            "La sincronización se lanza desde su pantalla, donde verás qué cuentas pueden leerse y cuáles no.",
            ok=False,
            links=[
                {
                    "label": "Analítica" if target == "metrics" else "Comunidad",
                    "href": page,
                }
            ],
        )

    if project is None:
        return _which(candidates)

    try:
        if step_kind:
            step = engine.steps_of(session, project.id)[step_kind]
            title = STEP_BY_KIND[step_kind].title
            if intent == "rerun":
                engine.rerun_step(session, project, step, user_id, user_name)
                return Reply(
                    f"Hecho: vuelvo a lanzar «{title}» en «{project.title}».",
                    links=[_project_link(project, step_kind)],
                    action={"type": "rerun", "step": step_kind},
                )
            if intent == "skip":
                engine.skip_step(session, project, step, user_name)
                return Reply(
                    f"Hecho: «{title}» queda omitido en «{project.title}».",
                    links=[_project_link(project)],
                    action={"type": "skip", "step": step_kind},
                )
            if intent == "pause":
                engine.pause_step(session, project, step, user_name)
                return Reply(
                    f"Hecho: «{title}» queda en pausa en «{project.title}».",
                    links=[_project_link(project, step_kind)],
                    action={"type": "pause_step", "step": step_kind},
                )
            if intent == "resume":
                engine.resume_step(session, project, step, user_name)
                return Reply(
                    f"Hecho: reanudo «{title}» en «{project.title}».",
                    links=[_project_link(project, step_kind)],
                    action={"type": "resume_step", "step": step_kind},
                )
        if step_kind and intent == "cancel":
            return Reply(
                f"Un paso no se cancela: puedo pausarlo u omitirlo. «Cancela» detiene el proyecto entero; si es lo que quieres, di «cancela «{project.title}»».",
                ok=False,
                links=[_project_link(project, step_kind)],
            )
        if intent == "rerun":
            failed = [
                s
                for s in engine.steps_of(session, project.id).values()
                if s.status in ("error", "cancelled")
            ]
            if not failed:
                options = ", ".join(s.title.lower() for s in STEPS[:7])
                return Reply(
                    f"En «{project.title}» no hay ningún paso con error. Dime cuál quieres relanzar: {options}.",
                    ok=False,
                    links=[_project_link(project)],
                )
            for step in sorted(failed, key=lambda s: s.position):
                engine.rerun_step(session, project, step, user_id, user_name)
            names = ", ".join(f"«{STEP_BY_KIND[s.kind].title}»" for s in failed)
            return Reply(
                f"Hecho: reintento {names} en «{project.title}».",
                links=[_project_link(project)],
                action={"type": "rerun"},
            )
        if intent == "skip":
            return Reply(
                "¿Qué paso omito? Dímelo por su nombre, por ejemplo «omite diseño».",
                ok=False,
                links=[_project_link(project)],
            )
        if intent == "pause":
            engine.pause_project(session, project, user_name)
            return Reply(
                f"Hecho: «{project.title}» queda en pausa. El trabajo en curso se detiene. Di «reanuda» para continuar.",
                links=[_project_link(project)],
                action={"type": "pause"},
            )
        if intent == "resume":
            engine.resume_project(session, project, user_name)
            return Reply(
                f"Hecho: «{project.title}» vuelve a estar activo.",
                links=[_project_link(project)],
                action={"type": "resume"},
            )
        if intent == "cancel":
            engine.cancel_project(session, project, user_name)
            return Reply(
                f"Hecho: «{project.title}» queda cancelado. No se borra nada; di «reanuda» si quieres recuperarlo.",
                links=[_project_link(project)],
                action={"type": "cancel"},
            )
    except engine.EngineError as exc:
        return Reply(f"No se puede: {exc}", ok=False, links=[_project_link(project)])
    return Reply("No he entendido la orden. " + HELP, ok=False)


def _mentions(plain: str, project: Project) -> bool:
    """True when the order names the project (as opposed to it being the only one)."""
    words = [w for w in normalize(project.title).split() if len(w) > 3]
    return any(re.search(rf"\b{re.escape(w)}\b", plain) for w in words)
