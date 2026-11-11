"""Projects, their pipeline steps and versioned documents."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from creator_office.agents import context, schemas
from creator_office.agents.steps import copy_checks, count_placeholders, script_summary
from creator_office.api import serializers
from creator_office.api.deps import ApiError, Auth, Current, Db, Editor, project_of
from creator_office.database import utcnow
from creator_office.models import (
    Asset,
    Clip,
    Comment,
    Design,
    Document,
    Metric,
    Notification,
    Project,
    Publication,
    Recommendation,
    Render,
    Run,
    Step,
)
from creator_office.pipeline import engine
from creator_office.pipeline.catalog import (
    LANGUAGES,
    PLATFORMS,
    STEP_BY_KIND,
    TEMPLATE_BY_ID,
)
from creator_office.security import UnsafeUrlError, assert_public_url
from creator_office.services import documents, production

router = APIRouter(prefix="/api", tags=["projects"])


class Reference(BaseModel):
    url: str = Field(max_length=2000)
    note: str = Field(default="", max_length=300)


class ProjectInput(BaseModel):
    title: str = Field(min_length=3, max_length=200)
    topic: str = Field(default="", max_length=4000)
    audience: str = Field(default="", max_length=2000)
    goal: str = Field(default="", max_length=2000)
    language: str = "es"
    tone: str = Field(default="", max_length=120)
    platforms: list[str] = Field(default_factory=list)
    deadline: datetime | None = None
    references: list[Reference] = Field(default_factory=list, max_length=12)
    template: str = "repurpose"
    options: dict[str, Any] = Field(default_factory=dict)


class ProjectPatch(BaseModel):
    title: str | None = Field(default=None, min_length=3, max_length=200)
    topic: str | None = Field(default=None, max_length=4000)
    audience: str | None = Field(default=None, max_length=2000)
    goal: str | None = Field(default=None, max_length=2000)
    language: str | None = None
    tone: str | None = Field(default=None, max_length=120)
    platforms: list[str] | None = None
    deadline: datetime | None = None
    clear_deadline: bool = False
    references: list[Reference] | None = Field(default=None, max_length=12)
    options: dict[str, Any] | None = None


def _clean_platforms(values: list[str]) -> list[str]:
    cleaned = [p for p in dict.fromkeys(values) if p in PLATFORMS]
    if not cleaned:
        raise ApiError(
            422,
            "Elige al menos una plataforma de destino.",
            "no_platforms",
            field="platforms",
        )
    return cleaned


def _clean_references(values: list[Reference]) -> list[dict[str, str]]:
    out = []
    for ref in values:
        url = ref.url.strip()
        if not url:
            continue
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        try:
            assert_public_url(url)
        except UnsafeUrlError as exc:
            raise ApiError(
                422,
                f"El enlace «{url[:80]}» no se puede usar: {exc}",
                "bad_reference",
                field="references",
            ) from exc
        out.append({"url": url, "note": ref.note.strip()})
    return out


def _clean_options(raw: dict[str, Any], template: str) -> dict[str, int]:
    base = dict(TEMPLATE_BY_ID[template].options)

    def number(key: str, low: int, high: int) -> int:
        try:
            return max(low, min(high, int(raw.get(key, base.get(key, low)))))
        except (TypeError, ValueError):
            return int(base.get(key, low))

    options = {
        "clip_count": number("clip_count", 1, 12),
        "clip_min_s": number("clip_min_s", 3, 600),
    }
    options["clip_max_s"] = max(
        options["clip_min_s"] + 2, number("clip_max_s", 5, 1800)
    )
    return options


def _deadline(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=utcnow().tzinfo)


@router.get("/projects")
def list_projects(auth: Current, db: Db) -> dict[str, Any]:
    return {"projects": serializers.project_list(db, auth.workspace_id)}


@router.post("/projects")
def create_project(body: ProjectInput, auth: Editor, db: Db) -> dict[str, Any]:
    if body.template not in TEMPLATE_BY_ID:
        raise ApiError(422, "Plantilla desconocida.", "bad_template", field="template")
    if body.language not in LANGUAGES:
        raise ApiError(422, "Idioma no admitido.", "bad_language", field="language")
    project = Project(
        workspace_id=auth.workspace_id,
        title=" ".join(body.title.split()),
        topic=body.topic.strip(),
        audience=body.audience.strip(),
        goal=body.goal.strip(),
        language=body.language,
        tone=body.tone.strip(),
        platforms=_clean_platforms(body.platforms),
        deadline=_deadline(body.deadline),
        references=_clean_references(body.references),
        template=body.template,
        options=_clean_options(body.options, body.template),
        created_by=auth.user.id,
    )
    db.add(project)
    db.flush()
    engine.create_steps(db, project)
    auth.log(
        db,
        "project.created",
        f"{auth.user.name} creó el proyecto «{project.title}».",
        project_id=project.id,
        data={"template": project.template, "platforms": project.platforms},
    )
    engine.advance(db, project)
    return serializers.project_detail(db, project)


@router.get("/projects/{project_id}")
def get_project(project_id: str, auth: Current, db: Db) -> dict[str, Any]:
    return serializers.project_detail(db, project_of(db, auth, project_id))


@router.patch("/projects/{project_id}")
def update_project(
    project_id: str, body: ProjectPatch, auth: Editor, db: Db
) -> dict[str, Any]:
    project = project_of(db, auth, project_id)
    changed: list[str] = []
    labels = {
        "title": "título",
        "topic": "tema",
        "audience": "audiencia",
        "goal": "objetivo",
        "tone": "tono",
    }
    for key, label in labels.items():
        value = getattr(body, key)
        if value is not None and value.strip() != getattr(project, key):
            setattr(
                project,
                key,
                " ".join(value.split()) if key == "title" else value.strip(),
            )
            changed.append(label)
    if body.language is not None and body.language != project.language:
        if body.language not in LANGUAGES:
            raise ApiError(422, "Idioma no admitido.", "bad_language", field="language")
        project.language = body.language
        changed.append("idioma")
    if body.platforms is not None:
        platforms = _clean_platforms(body.platforms)
        if platforms != list(project.platforms):
            project.platforms = platforms
            changed.append("plataformas")
    if body.clear_deadline and project.deadline is not None:
        project.deadline = None
        changed.append("fecha límite")
    elif body.deadline is not None:
        project.deadline = _deadline(body.deadline)
        changed.append("fecha límite")
    if body.references is not None:
        references = _clean_references(body.references)
        if references != list(project.references or []):
            project.references = references
            changed.append("referencias")
    if body.options is not None:
        options = _clean_options(body.options, project.template)
        if options != dict(project.options or {}):
            project.options = options
            changed.append("opciones de clips")
    if changed:
        auth.log(
            db,
            "project.updated",
            f"{auth.user.name} cambió en «{project.title}»: {', '.join(changed)}.",
            project_id=project.id,
        )
        # A new brief does not silently redo work: finished steps are flagged so a person decides.
        for step in engine.steps_of(db, project.id).values():
            if (
                step.status in ("completed", "awaiting_approval")
                and not step.stale_reason
                and STEP_BY_KIND[step.kind].trigger == "auto"
            ):
                step.stale_reason = (
                    f"El brief cambió ({', '.join(changed)}) después de este paso."
                )
        engine.advance(db, project)
    return serializers.project_detail(db, project)


def delete_project_data(db: Session, project: Project) -> None:
    """Remove a project with every file and record that hangs from it."""
    for step in engine.steps_of(db, project.id).values():
        engine.stop_runs(db, step)
    for run in db.scalars(
        select(Run).where(
            Run.project_id == project.id, Run.status.in_(("queued", "working"))
        )
    ):
        run.cancel_requested = True
        context.request_cancel(run.id)
    for asset in list(db.scalars(select(Asset).where(Asset.project_id == project.id))):
        if asset.kind == "poster":
            continue  # removed together with the asset it previews
        production.delete_asset(db, asset)
    for leftover in list(
        db.scalars(select(Asset).where(Asset.project_id == project.id))
    ):
        production.delete_asset(db, leftover)
    publication_ids = list(
        db.scalars(select(Publication.id).where(Publication.project_id == project.id))
    )
    if publication_ids:
        db.execute(delete(Metric).where(Metric.publication_id.in_(publication_ids)))
    for document in list(
        db.scalars(select(Document).where(Document.project_id == project.id))
    ):
        db.delete(document)
    for model in (Publication, Comment, Clip, Render, Design, Run):
        db.execute(delete(model).where(model.project_id == project.id))
    db.execute(delete(Recommendation).where(Recommendation.project_id == project.id))
    db.execute(delete(Step).where(Step.project_id == project.id))
    # Notices that point into the project would lead nowhere once it is gone.
    db.execute(
        delete(Notification)
        .where(
            Notification.workspace_id == project.workspace_id,
            Notification.link.like(f"/proyectos/{project.id}%"),
        )
        .execution_options(synchronize_session=False)
    )
    db.delete(project)


@router.delete("/projects/{project_id}")
def delete_project(project_id: str, auth: Editor, db: Db) -> dict[str, bool]:
    project = project_of(db, auth, project_id)
    title = project.title
    delete_project_data(db, project)
    auth.log(
        db,
        "project.deleted",
        f"{auth.user.name} eliminó el proyecto «{title}» y todos sus archivos.",
    )
    return {"ok": True}


class StateChange(BaseModel):
    action: str  # pause | resume | cancel


@router.post("/projects/{project_id}/state")
def change_state(
    project_id: str, body: StateChange, auth: Editor, db: Db
) -> dict[str, Any]:
    project = project_of(db, auth, project_id)
    actions = {
        "pause": engine.pause_project,
        "resume": engine.resume_project,
        "cancel": engine.cancel_project,
    }
    if body.action not in actions:
        raise ApiError(422, "Acción desconocida.", "bad_action")
    try:
        actions[body.action](db, project, auth.user.name)
    except engine.EngineError as exc:
        raise ApiError(409, str(exc), "not_possible") from exc
    return serializers.project_detail(db, project)


# --- Steps -------------------------------------------------------------------


class StepAction(BaseModel):
    action: str  # approve | request_changes | rerun | skip | pause | resume
    feedback: str = Field(default="", max_length=2000)
    chosen_angle: str | None = Field(default=None, max_length=300)
    custom_angle: str | None = Field(default=None, max_length=300)


def _step(db: Session, project: Project, kind: str) -> Step:
    step = engine.steps_of(db, project.id).get(kind)
    if step is None:
        raise ApiError(404, "Ese paso no existe en el proyecto.", "not_found")
    return step


@router.post("/projects/{project_id}/steps/{kind}")
def step_action(
    project_id: str, kind: str, body: StepAction, auth: Editor, db: Db
) -> dict[str, Any]:
    project = project_of(db, auth, project_id)
    step = _step(db, project, kind)
    name = auth.user.name
    try:
        if body.action == "approve":
            engine.approve_step(
                db,
                project,
                step,
                auth.user.id,
                name,
                {"chosen_angle": body.chosen_angle, "custom_angle": body.custom_angle},
            )
        elif body.action == "request_changes":
            engine.request_changes(db, project, step, auth.user.id, name, body.feedback)
        elif body.action == "rerun":
            engine.rerun_step(db, project, step, auth.user.id, name)
        elif body.action == "skip":
            engine.skip_step(db, project, step, name)
        elif body.action == "pause":
            engine.pause_step(db, project, step, name)
        elif body.action == "resume":
            engine.resume_step(db, project, step, name)
        else:
            raise ApiError(422, "Acción desconocida.", "bad_action")
    except engine.EngineError as exc:
        raise ApiError(409, str(exc), "not_possible") from exc
    return serializers.project_detail(db, project)


class StepInstructions(BaseModel):
    instructions: str = Field(max_length=2000)


@router.patch("/projects/{project_id}/steps/{kind}")
def step_instructions(
    project_id: str, kind: str, body: StepInstructions, auth: Editor, db: Db
) -> dict[str, Any]:
    project = project_of(db, auth, project_id)
    step = _step(db, project, kind)
    step.instructions = body.instructions.strip()
    auth.log(
        db,
        "step.instructions",
        f"{auth.user.name} dejó instrucciones para «{STEP_BY_KIND[kind].title}».",
        project_id=project.id,
        data={"kind": kind},
    )
    return serializers.project_detail(db, project)


# --- Documents ---------------------------------------------------------------


def _document(db: Session, project: Project, kind: str) -> Document:
    if kind not in ("research", "script"):
        raise ApiError(404, "Documento desconocido.", "not_found")
    document = documents.get_document(db, project.id, kind)
    if document is None:
        raise ApiError(404, "Todavía no existe este documento.", "not_found")
    return document


@router.get("/projects/{project_id}/documents/{kind}")
def document_versions(
    project_id: str, kind: str, auth: Current, db: Db
) -> dict[str, Any]:
    project = project_of(db, auth, project_id)
    document = _document(db, project, kind)
    return {
        "kind": kind,
        "title": document.title,
        "versions": [
            documents.version_to_dict(v, current_id=document.current_version_id)
            for v in documents.versions(db, document)
        ],
    }


class ScriptEdit(BaseModel):
    content: dict[str, Any]
    note: str = Field(default="", max_length=300)


def _save_script(
    db: Session, auth: Auth, project: Project, content: dict[str, Any], note: str
) -> None:
    try:
        parsed = schemas.ScriptOutput.model_validate(content)
    except ValidationError as exc:
        raise ApiError(
            422,
            "El guion no tiene el formato esperado.",
            "bad_document",
            errors=exc.errors()[:5],
        ) from exc
    data = parsed.model_dump()
    data["platform_copy"] = [
        c for c in data["platform_copy"] if c["platform"] in project.platforms
    ]
    data["copy_checks"] = copy_checks(data["platform_copy"])
    data["placeholders"] = count_placeholders(data)
    documents.save_version(
        db,
        workspace_id=project.workspace_id,
        project_id=project.id,
        kind="script",
        content=data,
        author_type="user",
        author=auth.user.name,
        note=note,
    )
    step = engine.steps_of(db, project.id)["script"]
    if step.status in ("completed", "awaiting_approval"):
        # The step's own line must describe the version that is now current, not the one the agent wrote.
        step.summary = script_summary(data)
        step.output = {
            **(step.output or {}),
            "sections": len(data["sections"]),
            "placeholders": data["placeholders"],
        }
    if step.status == "completed":
        engine.mark_stale(
            db, project, "script", "El guion se editó después de este paso."
        )


@router.post("/projects/{project_id}/documents/script")
def edit_script(
    project_id: str, body: ScriptEdit, auth: Editor, db: Db
) -> dict[str, Any]:
    project = project_of(db, auth, project_id)
    _document(db, project, "script")
    _save_script(db, auth, project, body.content, body.note.strip())
    auth.log(
        db,
        "document.edited",
        f"{auth.user.name} editó el guion de «{project.title}».",
        project_id=project.id,
        data={"kind": "script"},
    )
    return serializers.project_detail(db, project)


class Restore(BaseModel):
    version_id: str


@router.post("/projects/{project_id}/documents/{kind}/restore")
def restore_version(
    project_id: str, kind: str, body: Restore, auth: Editor, db: Db
) -> dict[str, Any]:
    project = project_of(db, auth, project_id)
    document = _document(db, project, kind)
    version = next(
        (v for v in documents.versions(db, document) if v.id == body.version_id), None
    )
    if version is None:
        raise ApiError(404, "Esa versión no existe.", "not_found")
    note = f"Restaurada la versión {version.number}"
    if kind == "script":
        _save_script(db, auth, project, dict(version.content), note)
    else:
        documents.save_version(
            db,
            workspace_id=project.workspace_id,
            project_id=project.id,
            kind=kind,
            content=dict(version.content),
            author_type="user",
            author=auth.user.name,
            note=note,
            provider=version.provider,
        )
        step = engine.steps_of(db, project.id)["research"]
        if step.status == "completed":
            engine.mark_stale(
                db,
                project,
                "research",
                "Se restauró otra versión de la investigación después de este paso.",
            )
    auth.log(
        db,
        "document.restored",
        f"{auth.user.name} restauró la versión {version.number} de «{document.title}».",
        project_id=project.id,
        data={"kind": kind, "version": version.number},
    )
    return serializers.project_detail(db, project)
