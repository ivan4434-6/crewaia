"""The built-in demo engine's playbook.

When no AI provider is configured, agents still run through CrewAI with a
local, deterministic "model" (see ``demo_llm``). It is not a language model:
it calls each agent's real tool and then fills a template from the project
brief and the tool's measurements. It does not write prose, judge content or
know anything about the world, so wherever a person or a real model has to
write, it leaves a marker in [square brackets] instead of making text up.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import date
from typing import Any
from urllib.parse import urlparse

from creator_office.agents.context import RunContext
from creator_office.services import analytics
from creator_office.text import clip_title, number, plural

Action = tuple[
    str, str, dict[str, Any]
]  # ("tool", name, args) | ("final", "", payload)

_STOPWORDS = set(
    "a al algo ante antes como con contra cual cuando de del desde donde durante e el ella ellas ellos en entre era es esa ese eso "
    "esta este esto ha hasta hay la las le les lo los mas más me mi mis mucho muy no nos o os otra otro para pero poco por porque que "
    "qué se sea ser si sí sin sobre su sus te tu tus un una uno unos y ya the and for with from that this your you are how what why "
    "into about more most to of in on at by an is it be as or we our vídeo video videos vídeos "
    "cómo cuál cuáles quién dónde cuándo cuánto usar usa usas hacer hace haces tener tiene tienes puede puedes poder todo toda "
    "todos todas cada así aquí ahora hoy mejor mejores nuevo nueva nuevos nuevas gran grande forma manera cosas cosa parte "
    "canal best make made using used will can".split()
)

# Words a headline must not end with when it is cut short.
_DANGLING = set(
    "a al con de del el en la las lo los o para por que se su sus tu tus un una y e u mi mis the of to in on for and with your a an".split()
)


def _english(ctx: RunContext) -> bool:
    return ctx.brief.get("language") == "en"


def keywords_from(text: str, limit: int = 6) -> list[str]:
    words = re.findall(r"[^\W\d_]{4,}", text.lower())
    out: list[str] = []
    for word in words:
        if word not in _STOPWORDS and word not in out:
            out.append(word)
    return out[:limit]


def _tool(name: str, args: dict[str, Any] | None = None) -> Action:
    return ("tool", name, args or {})


def _final(payload: dict[str, Any]) -> Action:
    return ("final", "", payload)


def _failed(ctx: RunContext, tool: str) -> str | None:
    result = ctx.tool_results.get(tool)
    return (
        str(result["error"])
        if isinstance(result, dict) and result.get("error")
        else None
    )


# --- Scout -------------------------------------------------------------------


def source_confidence(
    item: dict[str, Any], today: date | None = None
) -> tuple[str, str]:
    """Transparent rule: how much can a single source be trusted on its face."""
    published = item.get("published")
    if not published:
        return "baja", "La fuente no declara fecha de publicación."
    try:
        age_days = ((today or date.today()) - date.fromisoformat(published)).days
    except ValueError:
        return "baja", "La fecha de la fuente no se pudo interpretar."
    if age_days > 730:
        return "baja", f"Publicado hace más de dos años ({published})."
    return (
        "media",
        f"Fuente leída directamente, publicada el {published}. Una sola fuente: contrástala antes de afirmarlo.",
    )


def research(ctx: RunContext) -> Action:
    if "fetch_sources" not in ctx.tool_calls:
        return _tool("fetch_sources")
    brief = ctx.brief
    fetched = ctx.tool_results.get("fetch_sources") or {}
    items = fetched.get("items") or []
    english = _english(ctx)
    topic = brief.get("topic") or brief.get("title", "")
    keywords = keywords_from(f"{brief.get('title', '')} {topic}")
    findings = []
    for item in items[:10]:
        text = item.get("description") or item.get("excerpt", "")[:280]
        if not text:
            continue
        confidence, reason = source_confidence(item)
        findings.append(
            {
                "claim": f"«{text}»",
                "source_title": item.get("title", ""),
                "url": item["url"],
                "published": item.get("published"),
                "confidence": confidence,
                "confidence_reason": reason
                + (
                    " Cita literal de la fuente."
                    if not english
                    else " Literal quote from the source."
                ),
            }
        )

    def related(urls_limit: int = 3) -> list[str]:
        out = []
        for item in items:
            haystack = f"{item.get('title', '')} {item.get('description', '')}".lower()
            if any(word in haystack for word in keywords):
                out.append(item["url"])
        return out[:urls_limit]

    audience = brief.get("audience", "")
    if english:
        frames = [
            (
                f"Quick guide: {topic}",
                "Explainer format: useful when the audience wants the basics fast.",
            ),
            (
                f"Common mistakes with {topic}",
                "Mistake-led format: tends to suit audiences who already tried and got stuck.",
            ),
            (
                f"{topic}: the questions people keep asking",
                "Q&A format: works when you already receive questions on this subject.",
            ),
        ]
        note = "Demo mode: these angles are generic editorial formats applied to your topic. No AI model wrote or evaluated them."
        unverified = [
            "No trend or search-volume data was consulted: the demo engine has no access to any."
        ]
        if not items:
            unverified.append(
                "No source could be read, so no angle is backed by evidence."
            )
    else:
        frames = [
            (
                f"Guía rápida: {topic}",
                "Formato explicativo: útil cuando la audiencia quiere lo básico en poco tiempo.",
            ),
            (
                f"Errores frecuentes con {topic}",
                "Formato de errores: suele encajar con quien ya lo intentó y se atascó.",
            ),
            (
                f"{topic}: las preguntas que más se repiten",
                "Formato de preguntas y respuestas: funciona si ya recibes dudas sobre el tema.",
            ),
        ]
        note = (
            "Modo de demostración: estos ángulos son formatos editoriales genéricos aplicados a tu tema. "
            "Ningún modelo de IA los ha redactado ni evaluado."
        )
        unverified = [
            "No se han consultado datos de tendencias ni de volumen de búsqueda: el motor de demostración no tiene acceso a ellos."
        ]
        if not items:
            unverified.append(
                "No se pudo leer ninguna fuente, así que ningún ángulo está respaldado por datos."
            )
    urls = related()
    return _final(
        {
            "angles": [
                {
                    "title": title,
                    "rationale": why,
                    "audience": audience,
                    "keywords": keywords[:4],
                    "source_urls": urls,
                }
                for title, why in frames
            ],
            "findings": findings,
            "keywords": keywords,
            "unverified": unverified,
            "notes": note,
        }
    )


# --- Writer ------------------------------------------------------------------


def _script_payload(ctx: RunContext) -> dict[str, Any]:
    brief = ctx.brief
    english = _english(ctx)
    topic = brief.get("topic") or brief.get("title", "")
    angle = brief.get("angle") or brief.get("title", "")
    goal = brief.get("goal", "")
    structure = brief.get("structure") or ["Gancho", "Desarrollo", "Cierre"]
    keywords = keywords_from(f"{brief.get('title', '')} {topic}")
    hashtags = list(
        dict.fromkeys(
            [*brief.get("brand_hashtags", []), *[f"#{k}" for k in keywords[:4]]]
        )
    )[:8]
    if english:
        sections = [
            {
                "heading": heading,
                "text": f"[Write the “{heading}” part here, in your own words, for: {angle}]",
                "visual": "[On-screen text or shot]",
            }
            for heading in structure
        ]
        hooks = [
            f"What nobody tells you about {topic}: [your fact or experience]",
            f"If you care about {topic}, avoid this mistake: [specific mistake]",
            f"[Concrete result] in [time]: this is how {topic} works",
        ]
        cta = f"[Call to action — what should the viewer do? Your goal: “{goal}”]"
        note = "Template draft produced without an AI model. Replace every text in [square brackets]."
    else:
        sections = [
            {
                "heading": heading,
                "text": f"[Escribe aquí la parte «{heading}», con tus palabras, para: {angle}]",
                "visual": "[Texto en pantalla o plano]",
            }
            for heading in structure
        ]
        hooks = [
            f"Lo que nadie te cuenta sobre {topic}: [tu dato o experiencia]",
            f"Si te interesa {topic}, evita este error: [error concreto]",
            f"[Resultado concreto] en [tiempo]: así funciona {topic}",
        ]
        cta = f"[Llamada a la acción: ¿qué quieres que haga quien lo ve? Tu objetivo: «{goal}»]"
        note = "Borrador de plantilla generado sin modelo de IA. Sustituye todos los textos entre [corchetes]."
    if brief.get("feedback"):
        note += (
            " Requested changes on file: "
            if english
            else " Cambios pedidos que debes aplicar a mano: "
        ) + brief["feedback"]
    title = str(brief.get("title", ""))[:100]
    return {
        "working_title": angle,
        "hooks": hooks,
        "outline": list(structure),
        "sections": sections,
        "cta": cta,
        "titles": list(dict.fromkeys([title, angle]))[:3],
        "platform_copy": [
            {
                "platform": platform,
                "title": title,
                "description": topic,
                "hashtags": hashtags,
            }
            for platform in brief.get("platforms", [])
        ],
        "notes": note,
    }


def script(ctx: RunContext) -> Action:
    payload = _script_payload(ctx)
    if "check_platform_copy" not in ctx.tool_calls:
        return _tool("check_platform_copy", {"items": payload["platform_copy"]})
    return _final(payload)


# --- Clipper -----------------------------------------------------------------


def clips(ctx: RunContext) -> Action:
    if "analyze_video" not in ctx.tool_calls:
        return _tool("analyze_video")
    analysis = ctx.tool_results.get("analyze_video") or {}
    if _failed(ctx, "analyze_video"):
        return _final({"picks": [], "notes": _failed(ctx, "analyze_video")})
    english = _english(ctx)
    picks = []
    for candidate in analysis.get("candidates", [])[
        : analysis.get("requested_clips", 5)
    ]:
        excerpt = candidate.get("excerpt", "")
        words = excerpt.split()
        title = (
            " ".join(words[:7]).rstrip(" ,;:.-") + ("…" if len(words) > 7 else "")
            if words
            else clip_title(candidate["start"], candidate["end"])
        )
        reason = candidate.get("facts", "")
        if excerpt:
            reason += (
                " Transcript: " if english else " Texto del tramo: "
            ) + f"«{excerpt}»"
        picks.append(
            {"candidate_id": candidate["id"], "title": title, "reason": reason}
        )
    note = (
        "Demo mode: clips are ranked only by measured audio and picture signals. Nobody has judged what is said."
        if english
        else "Modo de demostración: los clips se ordenan solo por las señales medidas de audio e imagen. Nadie ha valorado lo que se dice."
    )
    return _final({"picks": picks, "notes": note})


# --- Editor ------------------------------------------------------------------


def edit(ctx: RunContext) -> Action:
    if "render_pieces" not in ctx.tool_calls:
        brief = ctx.brief
        source_vertical = brief.get("source_height", 0) > brief.get("source_width", 0)
        pieces = []
        for clip in brief.get("clips", []):
            for fmt in brief.get("formats", []):
                same_shape = (fmt == "vertical") == source_vertical and fmt != "square"
                pieces.append(
                    {
                        "clip_id": clip["id"],
                        "format": fmt,
                        "framing": "crop" if same_shape else "blur",
                        "subtitles": bool(
                            brief.get("subtitles_default")
                            and brief.get("transcript_available")
                        ),
                    }
                )
        return _tool("render_pieces", {"pieces": pieces})
    result = ctx.tool_results.get("render_pieces") or {}
    done = [r for r in result.get("rendered", []) if r.get("status") == "ready"]
    english = _english(ctx)
    return _final(
        {
            "summary": (
                f"{len(done)} pieces exported."
                if english
                else f"{len(done)} piezas exportadas."
            ),
            "notes": (
                "Demo mode: framing uses a blurred background whenever the source shape differs, so nothing is cropped out. Adjust it per piece."
                if english
                else "Modo de demostración: cuando la forma del original no coincide se usa fondo desenfocado para no recortar nada. Ajústalo en cada pieza."
            ),
        }
    )


# --- Designer ----------------------------------------------------------------


def short_headline(text: str, max_words: int = 6, max_chars: int = 42) -> str:
    words = text.replace("\n", " ").split()
    out: list[str] = []
    for word in words:
        if len(out) >= max_words or len(" ".join([*out, word])) > max_chars:
            break
        out.append(word)
    if len(out) < len(words):  # cut short: do not leave it hanging on "…DE", "…EN"
        while len(out) > 1 and out[-1].lower().strip(":;,.-¿?¡!") in _DANGLING:
            out.pop()
    return (" ".join(out) or text[:max_chars]).upper().rstrip(":;,.-")


def design(ctx: RunContext) -> Action:
    if "compose_designs" not in ctx.tool_calls:
        brief = ctx.brief
        headline = short_headline(
            brief.get("headline_source") or brief.get("title", "")
        )
        return _tool(
            "compose_designs",
            {
                "designs": [
                    {
                        "kind": kind,
                        "headline": headline,
                        "layout": brief.get("brand_layout", "side")
                        if kind == "thumbnail"
                        else "band",
                        "background": "frame" if brief.get("has_video") else "gradient",
                    }
                    for kind in brief.get("design_kinds", [])
                ]
            },
        )
    result = ctx.tool_results.get("compose_designs") or {}
    done = [d for d in result.get("designs", []) if d.get("status") == "ready"]
    english = _english(ctx)
    return _final(
        {
            "summary": (
                f"{len(done)} designs composed with the brand kit."
                if english
                else f"{len(done)} diseños compuestos con el kit de marca."
            ),
            "notes": (
                "Demo mode: the headline is your project title, shortened. Edit it in the studio."
                if english
                else "Modo de demostración: el titular es el título de tu proyecto, acortado. Edítalo en el estudio."
            ),
        }
    )


# --- Publisher ---------------------------------------------------------------


def publish(ctx: RunContext) -> Action:
    if "save_drafts" not in ctx.tool_calls:
        brief = ctx.brief
        copy = {c["platform"]: c for c in brief.get("platform_copy", [])}
        per_platform: dict[str, int] = {}
        for draft in brief.get("drafts", []):
            per_platform[draft["platform"]] = per_platform.get(draft["platform"], 0) + 1
        seen: dict[str, int] = {}
        drafts = []
        for draft in brief.get("drafts", []):
            platform = draft["platform"]
            seen[platform] = seen.get(platform, 0) + 1
            base = copy.get(platform, {})
            suffix = (
                f" ({seen[platform]}/{per_platform[platform]})"
                if per_platform[platform] > 1
                else ""
            )
            limit = 100 - len(suffix)
            title = (base.get("title") or brief.get("title", ""))[:limit] + suffix
            from creator_office.agents.tools import platform_fields

            final_title, final_description = platform_fields(
                platform, title, base.get("description", ""), base.get("hashtags", [])
            )
            drafts.append(
                {
                    "publication_id": draft["id"],
                    "title": final_title,
                    "description": final_description,
                    "tags": [tag.lstrip("#") for tag in base.get("hashtags", [])],
                }
            )
        return _tool("save_drafts", {"drafts": drafts})
    report = (ctx.tool_results.get("save_drafts") or {}).get("report", [])
    ready = sum(1 for r in report if r.get("status") in ("pass", "warn"))
    english = _english(ctx)
    return _final(
        {
            "summary": (
                f"{len(report)} drafts saved; {ready} meet the platform requirements."
                if english
                else f"{len(report)} borradores guardados; {ready} cumplen los requisitos de su plataforma."
            ),
            "notes": (
                "Demo mode: texts come from the approved script. Nothing has been published."
                if english
                else "Modo de demostración: los textos salen del guion aprobado. No se ha publicado nada."
            ),
        }
    )


# --- Community ---------------------------------------------------------------


def community(ctx: RunContext) -> Action:
    if "load_comments" not in ctx.tool_calls:
        return _tool("load_comments")
    english = _english(ctx)
    items = []
    for comment in (ctx.tool_results.get("load_comments") or {}).get("comments", []):
        category = comment["rule_category"]
        author = comment.get("author", "").lstrip("@")
        name = f", {author}" if author else ""
        if english:
            reply = {
                "question": f"Thanks for asking{name}! [Write the answer here]",
                "positive": f"Thank you so much{name}! Glad you liked it.",
                "issue": f"Thanks for letting us know{name}. [Say what you will do, or ask for details]",
            }.get(category, "")
        else:
            reply = {
                "question": f"¡Gracias por tu pregunta{name}! [Escribe aquí la respuesta]",
                "positive": f"¡Muchas gracias{name}! Nos alegra que te haya gustado.",
                "issue": f"Gracias por avisar{name}. [Indica qué vas a hacer o pide más detalles]",
            }.get(category, "")
        items.append(
            {
                "comment_id": comment["comment_id"],
                "category": category,
                "reason": comment["rule_reason"],
                "reply": reply,
            }
        )
    return _final(
        {
            "items": items,
            "notes": (
                "Demo mode: categories come from keyword rules and replies are templates. Review each one."
                if english
                else "Modo de demostración: las categorías salen de reglas de palabras clave y las respuestas son plantillas. Revisa cada una."
            ),
        }
    )


# --- Analyst -----------------------------------------------------------------


def analysis(ctx: RunContext) -> Action:
    if "compute_stats" not in ctx.tool_calls:
        return _tool("compute_stats")
    stats = ctx.tool_results.get("compute_stats") or {}
    if _failed(ctx, "compute_stats") or "publications" not in stats:
        return _final(
            {
                "summary": "",
                "recommendations": [],
                "data_gaps": [_failed(ctx, "compute_stats") or "Sin datos."],
            }
        )
    recommendations, gaps = analytics.rule_recommendations(stats)
    totals = stats.get("totals", {})
    summary = f"{plural(stats['with_metrics'], 'pieza')} con métricas de {plural(stats['published_total'], 'publicada')}."
    if "views" in totals:
        summary += f" Visualizaciones sumadas: {number(totals['views'])}."
    summary += " Modo de demostración: las recomendaciones salen de reglas fijas aplicadas a tus cifras."
    return _final(
        {"summary": summary, "recommendations": recommendations, "data_gaps": gaps}
    )


PLANS: dict[str, Callable[[RunContext], Action]] = {
    "research": research,
    "script": script,
    "clips": clips,
    "edit": edit,
    "design": design,
    "publish": publish,
    "community": community,
    "analysis": analysis,
}


def host_of(url: str) -> str:
    return urlparse(url).hostname or url
