"""Rule-based comment triage and manual import parsing.

The rules are deliberately simple and transparent: each classification comes
with the rule that fired, so a person can see why and correct it.
"""

from __future__ import annotations

import csv
import io
import re
from typing import Any

CATEGORIES = {
    "question": "Pregunta",
    "positive": "Positivo",
    "spam": "Spam",
    "issue": "Incidencia",
    "other": "Otros",
}

_URL = re.compile(
    r"https?://|www\.|\b[\w-]+\.(?:com|net|org|io|ly|me|xyz|top|click)\b", re.I
)
_SPAM = re.compile(
    r"\b(?:gana dinero|ganar dinero|dinero f[aá]cil|inversi[oó]n garantizada|cripto|crypto|bitcoin|forex|whatsapp|telegram|"
    r"sígueme|sigueme|follow me|check my (?:channel|profile)|mi canal|suscr[ií]bete a mi|free followers|seguidores gratis|"
    r"promo code|c[oó]digo promo|earn money|make money|giveaway|sorteo en mi perfil|onlyfans)\b",
    re.I,
)
_ISSUE = re.compile(
    r"\b(?:no funciona|no se oye|no se escucha|no se ve|sin sonido|sin audio|error|fallo|falla|roto|enlace roto|link roto|"
    r"no carga|se corta|mal subtitulad[oa]|est[aá] mal|incorrecto|equivocad[oa]|estafa|denuncia|reembolso|queja|"
    r"doesn'?t work|not working|broken|no sound|can'?t hear|wrong|mistake|refund|scam)\b",
    re.I,
)
_QUESTION = re.compile(
    r"\?|¿|\b(?:c[oó]mo|qu[eé]|cu[aá]l|cu[aá]ndo|d[oó]nde|por qu[eé]|qui[eé]n|cu[aá]nto|alguien sabe|"
    r"how|what|which|when|where|why|who|can you|could you|does anyone)\b",
    re.I,
)
_POSITIVE = re.compile(
    r"\b(?:gracias|genial|excelente|incre[ií]ble|me encanta|me encant[oó]|me gust[aó]|buen[ií]sim[oa]|muy buen[oa]?|"
    r"buen (?:v[ií]deo|contenido|trabajo|canal)|gran (?:v[ií]deo|contenido|trabajo|canal)|me (?:ha servido|sirvi[oó]|ayud[oó]|ha ayudado)|"
    r"muy [uú]til|[uú]til|crack|grande|brutal|maravilla|perfecto|bravo|felicidades|enhorabuena|"
    r"thanks|thank you|great|awesome|amazing|love (?:it|this)|helpful|nice|perfect|well done)\b|[❤👏🔥😍🙌💯👍]",
    re.I,
)


def classify(text: str) -> tuple[str, str]:
    """Return (category, reason). Order matters: spam and issues win over tone."""
    has_link = bool(_URL.search(text))
    spam_word = _SPAM.search(text)
    if spam_word and has_link:
        return "spam", f"Contiene un enlace y la expresión «{spam_word.group(0)}»."
    if spam_word:
        return "spam", f"Contiene la expresión promocional «{spam_word.group(0)}»."
    issue = _ISSUE.search(text)
    if issue:
        return "issue", f"Menciona un posible problema: «{issue.group(0)}»."
    question = _QUESTION.search(text)
    if question:
        marker = question.group(0)
        return (
            "question",
            "Tiene signo de interrogación."
            if marker in "?¿"
            else f"Empieza a preguntar con «{marker}».",
        )
    positive = _POSITIVE.search(text)
    if positive:
        return "positive", f"Expresión positiva: «{positive.group(0)}»."
    if has_link:
        return (
            "other",
            "Contiene un enlace pero ninguna expresión promocional conocida: revísalo.",
        )
    return "other", "Ninguna regla coincide."


def parse_import(text: str) -> list[dict[str, Any]]:
    """Parse pasted comments.

    Accepts CSV with a header containing ``texto``/``text`` (and optionally
    ``autor``/``author``, ``fecha``/``date``), or plain lines in the form
    ``autor: comentario``.
    """
    text = text.replace("\ufeff", "").strip()
    if not text:
        return []
    first = text.splitlines()[0].lower()
    delimiter = ";" if first.count(";") > first.count(",") else ","
    headers = [h.strip().strip('"') for h in first.split(delimiter)]
    text_column = next(
        (h for h in headers if h in ("texto", "text", "comentario", "comment")), None
    )
    rows: list[dict[str, Any]] = []
    if text_column and len(headers) > 1:
        reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
        for row in reader:
            normal = {
                (k or "").strip().lower(): (v or "").strip() for k, v in row.items()
            }
            body = normal.get(text_column, "")
            if body:
                rows.append(
                    {
                        "author": normal.get("autor")
                        or normal.get("author")
                        or normal.get("usuario")
                        or "",
                        "text": body[:5000],
                        "posted_at": normal.get("fecha") or normal.get("date") or "",
                    }
                )
        return rows
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        author, body = "", line
        match = re.match(r"^@?([^:\s][^:]{0,60}):\s+(.+)$", line)
        if match and not match.group(1).lower().startswith(("http", "https")):
            author, body = match.group(1).strip(), match.group(2).strip()
        rows.append({"author": author, "text": body[:5000], "posted_at": ""})
    return rows
