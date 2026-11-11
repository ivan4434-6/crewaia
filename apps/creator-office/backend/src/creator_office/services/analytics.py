"""Statistics over the metrics that exist, and nothing else.

Figures come from two places only: a connected platform account or numbers a
person typed in. Each snapshot keeps that origin, missing fields are listed as
missing, and comparisons are only made when every group has enough pieces.
"""

from __future__ import annotations

from collections import defaultdict
from statistics import median
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from creator_office import clock
from creator_office.connectors.registry import CONNECTORS
from creator_office.models import Metric, Project, Publication, Render, Workspace
from creator_office.pipeline.catalog import PLATFORMS
from creator_office.text import choose, number, plural, shorten

# ``whole`` marks counts: "1.840" typed there can only mean 1840, and 1,84 views is refused.
METRIC_FIELDS: dict[str, dict[str, Any]] = {
    "views": {"label": "Visualizaciones", "unit": "", "whole": True},
    "watch_time_minutes": {
        "label": "Tiempo de visualización",
        "unit": "min",
        "whole": False,
    },
    "avg_view_duration_s": {
        "label": "Duración media de visualización",
        "unit": "s",
        "whole": False,
    },
    "retention_pct": {"label": "Retención media", "unit": "%", "whole": False},
    "likes": {"label": "Me gusta", "unit": "", "whole": True},
    "comments": {"label": "Comentarios", "unit": "", "whole": True},
    "shares": {"label": "Compartidos", "unit": "", "whole": True},
    "saves": {"label": "Guardados", "unit": "", "whole": True},
    "reach": {"label": "Alcance", "unit": "", "whole": True},
}
CORE_FIELDS = ("views", "retention_pct", "likes", "comments")
MIN_GROUP = 3


def _parse(key: str, value: Any) -> float | None:
    """A usable figure, or ``None`` when it is not a number in the field's range."""
    if isinstance(value, bool):
        return None
    try:
        figure = float(value)
    except (TypeError, ValueError):
        return None
    if figure != figure or figure in (float("inf"), float("-inf")):
        return None
    if figure < 0 or (key == "retention_pct" and figure > 100):
        return None
    if METRIC_FIELDS[key]["whole"] and figure != int(figure):
        return None
    return round(figure, 2)


def clean_values(raw: dict[str, Any]) -> dict[str, float]:
    """Keep what is usable. For data read from a platform, where an odd field must not lose the rest."""
    values: dict[str, float] = {}
    for key in METRIC_FIELDS:
        value = raw.get(key)
        if value in (None, ""):
            continue
        figure = _parse(key, value)
        if figure is not None:
            values[key] = figure
    return values


def invalid_fields(raw: dict[str, Any]) -> list[str]:
    """Labels of the figures a person typed that cannot be stored. Nothing is dropped in silence."""
    bad: list[str] = []
    for key, value in raw.items():
        if value in (None, ""):
            continue
        if key not in METRIC_FIELDS:
            bad.append(str(key)[:40])
        elif _parse(key, value) is None:
            bad.append(METRIC_FIELDS[key]["label"])
    return bad


def core_fields(platform: str) -> tuple[str, ...]:
    """The basic indicators this platform reports at all: what it never gives is not "missing"."""
    provider = PLATFORMS.get(platform, {}).get("provider")
    unavailable = (
        CONNECTORS[provider].CAPABILITIES.metrics_missing
        if provider in CONNECTORS
        else ()
    )
    return tuple(key for key in CORE_FIELDS if key not in unavailable)


def engagement_rate(values: dict[str, float]) -> float | None:
    views = values.get("views")
    if not views or "likes" not in values:
        return None
    interactions = sum(
        values.get(k, 0) for k in ("likes", "comments", "shares", "saves")
    )
    return round(interactions / views * 100, 2)


def _hour_bucket(hour: int) -> str:
    if 6 <= hour < 12:
        return "Mañana (6-12 h)"
    if 12 <= hour < 18:
        return "Tarde (12-18 h)"
    if 18 <= hour < 24:
        return "Noche (18-24 h)"
    return "Madrugada (0-6 h)"


def compute(
    session: Session, workspace_id: str, project_id: str | None = None
) -> dict[str, Any]:
    query = select(Publication).where(
        Publication.workspace_id == workspace_id, Publication.status == "published"
    )
    if project_id:
        query = query.where(Publication.project_id == project_id)
    publications = list(session.scalars(query))
    projects = {
        p.id: p
        for p in session.scalars(
            select(Project).where(Project.workspace_id == workspace_id)
        )
    }
    renders = {
        r.id: r
        for r in session.scalars(
            select(Render).where(Render.workspace_id == workspace_id)
        )
    }
    workspace = session.get(Workspace, workspace_id)
    settings = workspace.settings if workspace else None
    zone = clock.zone_name(settings)
    latest: dict[str, Metric] = {}
    history: dict[str, int] = defaultdict(int)
    if publications:
        rows = session.scalars(
            select(Metric)
            .where(Metric.publication_id.in_([p.id for p in publications]))
            .order_by(Metric.captured_at.asc(), Metric.created_at.asc())
        )
        for row in rows:
            latest[row.publication_id] = row
            history[row.publication_id] += 1

    items: list[dict[str, Any]] = []
    for pub in publications:
        metric = latest.get(pub.id)
        if metric is None:
            continue
        values = clean_values(metric.values)
        render = renders.get(pub.render_id or "")
        project = projects.get(pub.project_id)
        items.append(
            {
                "publication_id": pub.id,
                "project_id": pub.project_id,
                "project": project.title if project else "",
                "title": shorten(pub.title or pub.description, 80),
                "platform": pub.platform,
                "platform_name": PLATFORMS[pub.platform]["name"],
                "format": render.format if render else "",
                "published_at": pub.published_at.isoformat()
                if pub.published_at
                else None,
                "hour_bucket": _hour_bucket(
                    clock.local(pub.published_at, settings).hour
                )
                if pub.published_at
                else "",
                "confirmation": pub.confirmation,
                "source": metric.source,
                "captured_at": metric.captured_at.isoformat(),
                "snapshots": history[pub.id],
                "values": values,
                "engagement_rate": engagement_rate(values),
                "missing": [
                    METRIC_FIELDS[k]["label"]
                    for k in core_fields(pub.platform)
                    if k not in values
                ],
            }
        )

    totals: dict[str, float] = {}
    for key in ("views", "watch_time_minutes", "likes", "comments", "shares", "saves"):
        present = [i["values"][key] for i in items if key in i["values"]]
        if present:
            totals[key] = round(sum(present), 2)

    comparisons = [
        _compare(items, "platform_name", "Plataforma"),
        _compare(items, "format", "Formato"),
        _compare(items, "project", "Tema (proyecto)"),
        _compare(items, "hour_bucket", f"Hora de publicación ({clock.label(zone)})"),
    ]
    return {
        "published_total": len(publications),
        "with_metrics": len(items),
        "without_metrics": len(publications) - len(items),
        "sources": {
            "platform": sum(1 for i in items if i["source"] == "platform"),
            "manual": sum(1 for i in items if i["source"] == "manual"),
        },
        "incomplete": sum(1 for i in items if i["missing"]),
        "totals": totals,
        "publications": sorted(
            items, key=lambda i: i["values"].get("views", 0), reverse=True
        ),
        "comparisons": comparisons,
        "min_group": MIN_GROUP,
        "timezone": zone,
    }


def _compare(items: list[dict[str, Any]], key: str, label: str) -> dict[str, Any]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in items:
        if item.get(key) and "views" in item["values"]:
            groups[item[key]].append(item)
    summary = []
    for name, members in sorted(groups.items()):
        retention = [
            m["values"]["retention_pct"]
            for m in members
            if "retention_pct" in m["values"]
        ]
        engagement = [
            m["engagement_rate"] for m in members if m["engagement_rate"] is not None
        ]
        summary.append(
            {
                "name": name,
                "pieces": len(members),
                "views_median": median(m["values"]["views"] for m in members),
                "retention_avg": round(sum(retention) / len(retention), 1)
                if retention
                else None,
                "retention_pieces": len(retention),
                "engagement_avg": round(sum(engagement) / len(engagement), 2)
                if engagement
                else None,
                "engagement_pieces": len(engagement),
            }
        )
    enough = [g for g in summary if g["pieces"] >= MIN_GROUP]
    sufficient = len(enough) >= 2
    reason = ""
    if not sufficient:
        if len(summary) < 2:
            reason = "Solo hay un grupo con datos: no hay con qué comparar."
        else:
            reason = f"Se necesitan al menos {MIN_GROUP} piezas con visualizaciones en dos grupos o más para comparar."
    return {
        "dimension": key,
        "label": label,
        "sufficient": sufficient,
        "reason": reason,
        "groups": summary,
    }


def rule_recommendations(
    stats: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Recommendations that follow mechanically from the user's own figures."""
    recommendations: list[dict[str, Any]] = []
    gaps: list[str] = []
    items = stats["publications"]
    if not items:
        return [], [
            "Aún no hay métricas de ninguna publicación: conecta una cuenta o añade las cifras a mano."
        ]

    for comparison in stats["comparisons"]:
        if not comparison["sufficient"]:
            if len(comparison["groups"]) >= 2:
                gaps.append(f"{comparison['label']}: {comparison['reason']}")
            continue
        eligible = [g for g in comparison["groups"] if g["pieces"] >= MIN_GROUP]
        best = max(eligible, key=lambda g: g["views_median"])
        worst = min(eligible, key=lambda g: g["views_median"])
        if (
            best["name"] != worst["name"]
            and worst["views_median"] > 0
            and best["views_median"] >= worst["views_median"] * 1.25
        ):
            recommendations.append(
                {
                    "text": f"{comparison['label']}: «{best['name']}» consigue más visualizaciones por pieza que «{worst['name']}». "
                    f"Prioriza «{best['name']}» en los próximos contenidos y revisa si merece la pena mantener el otro.",
                    "evidence": [
                        f"«{best['name']}»: mediana de {number(best['views_median'])} visualizaciones en {best['pieces']} piezas.",
                        f"«{worst['name']}»: mediana de {number(worst['views_median'])} visualizaciones en {worst['pieces']} piezas.",
                    ],
                }
            )

    with_retention = [i for i in items if "retention_pct" in i["values"]]
    if len(with_retention) >= 2:
        best = max(with_retention, key=lambda i: i["values"]["retention_pct"])
        worst = min(with_retention, key=lambda i: i["values"]["retention_pct"])
        if best["values"]["retention_pct"] - worst["values"]["retention_pct"] >= 10:
            recommendations.append(
                {
                    "text": f"Compara los primeros segundos de «{best['title']}» y «{worst['title']}»: la primera retiene bastante más. "
                    "Identifica qué hace distinto su arranque y repítelo.",
                    "evidence": [
                        f"«{best['title']}» ({best['platform_name']}): {number(best['values']['retention_pct'])} % de retención media.",
                        f"«{worst['title']}» ({worst['platform_name']}): {number(worst['values']['retention_pct'])} % de retención media.",
                    ],
                }
            )
    elif items:
        gaps.append(
            "Retención: hacen falta al menos dos piezas con el dato de retención media para compararlas."
        )

    with_engagement = [i for i in items if i["engagement_rate"] is not None]
    if len(with_engagement) >= 2:
        best = max(with_engagement, key=lambda i: i["engagement_rate"])
        worst = min(with_engagement, key=lambda i: i["engagement_rate"])
        if best["engagement_rate"] >= max(0.01, worst["engagement_rate"]) * 1.5:
            recommendations.append(
                {
                    "text": f"«{best['title']}» genera más interacción por visualización que el resto. "
                    "Revisa su llamada a la acción y su tema para reutilizarlos.",
                    "evidence": [
                        f"«{best['title']}»: {number(best['engagement_rate'])} % de interacción (me gusta, comentarios, compartidos y guardados entre visualizaciones).",
                        f"«{worst['title']}»: {number(worst['engagement_rate'])} %.",
                    ],
                }
            )

    incomplete, measured = stats["incomplete"], stats["with_metrics"]
    if incomplete and measured == 1:
        gaps.append(
            "La única pieza con métricas tiene datos incompletos (falta algún indicador básico)."
        )
    elif incomplete:
        gaps.append(
            f"{incomplete} de {measured} piezas {choose(incomplete, 'tiene', 'tienen')} datos incompletos (falta algún indicador básico)."
        )
    unmeasured = stats["without_metrics"]
    if unmeasured:
        gaps.append(
            f"{plural(unmeasured, 'publicación', 'publicaciones')} {choose(unmeasured, 'no tiene', 'no tienen')} ninguna métrica todavía."
        )
    manual = stats["sources"]["manual"]
    if manual:
        gaps.append(
            f"{plural(manual, 'pieza')} {choose(manual, 'usa', 'usan')} cifras añadidas a mano: no están verificadas con la plataforma."
        )
    return recommendations, gaps
