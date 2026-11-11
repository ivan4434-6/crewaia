"""Versioned documents (research, script, publication texts)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from creator_office.models import Document, DocumentVersion

TITLES = {"research": "Investigación", "script": "Guion"}


def get_document(
    session: Session, project_id: str, kind: str, ref_id: str = ""
) -> Document | None:
    return session.scalar(
        select(Document).where(
            Document.project_id == project_id,
            Document.kind == kind,
            Document.ref_id == ref_id,
        )
    )


def current(
    session: Session, project_id: str, kind: str, ref_id: str = ""
) -> tuple[Document | None, DocumentVersion | None]:
    document = get_document(session, project_id, kind, ref_id)
    if document is None or not document.current_version_id:
        return document, None
    return document, session.get(DocumentVersion, document.current_version_id)


def current_content(session: Session, project_id: str, kind: str) -> dict[str, Any]:
    _, version = current(session, project_id, kind)
    return dict(version.content) if version else {}


def save_version(
    session: Session,
    *,
    workspace_id: str,
    project_id: str,
    kind: str,
    content: dict[str, Any],
    author_type: str,
    author: str,
    note: str = "",
    provider: str = "",
    ref_id: str = "",
    title: str = "",
) -> DocumentVersion:
    document = get_document(session, project_id, kind, ref_id)
    if document is None:
        document = Document(
            workspace_id=workspace_id,
            project_id=project_id,
            kind=kind,
            ref_id=ref_id,
            title=title or TITLES.get(kind, kind),
        )
        session.add(document)
        session.flush()
    number = (
        session.scalar(
            select(func.max(DocumentVersion.number)).where(
                DocumentVersion.document_id == document.id
            )
        )
        or 0
    ) + 1
    version = DocumentVersion(
        document_id=document.id,
        number=number,
        content=content,
        author_type=author_type,
        author=author,
        note=note,
        provider=provider,
    )
    session.add(version)
    session.flush()
    document.current_version_id = version.id
    return version


def versions(session: Session, document: Document) -> list[DocumentVersion]:
    return list(
        session.scalars(
            select(DocumentVersion)
            .where(DocumentVersion.document_id == document.id)
            .order_by(DocumentVersion.number.desc())
        )
    )


def version_to_dict(
    version: DocumentVersion,
    *,
    include_content: bool = True,
    current_id: str | None = None,
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": version.id,
        "number": version.number,
        "author_type": version.author_type,
        "author": version.author,
        "note": version.note,
        "provider": version.provider,
        "created_at": version.created_at.isoformat(),
        "is_current": version.id == current_id,
    }
    if include_content:
        data["content"] = version.content
    return data
