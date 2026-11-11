"""Static catalogue: the eight agents, the pipeline stages and project templates."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class AgentInfo:
    key: str
    name: str
    specialty: str
    description: str
    color: str
    # Prompt-facing identity handed to CrewAI.
    role: str
    goal: str
    backstory: str


AGENTS: dict[str, AgentInfo] = {
    a.key: a
    for a in (
        AgentInfo(
            "scout",
            "Scout",
            "Investigación y tendencias",
            "Lee las fuentes que tú permites y propone ángulos, audiencias y palabras clave. "
            "Cada hallazgo lleva enlace, fuente, fecha y nivel de confianza; lo que no puede verificar, lo dice.",
            "#5eead4",
            "Content research scout",
            "Propose content angles grounded only in sources that were actually read, and say plainly what could not be verified.",
            "You research topics for a small video team. You never invent trends, figures or sources: "
            "every factual claim must point to a URL returned by your tools.",
        ),
        AgentInfo(
            "writer",
            "Writer",
            "Guiones y textos",
            "Escribe ganchos, guiones, llamadas a la acción y variaciones por plataforma respetando la voz de marca. "
            "Todo se puede revisar y editar antes de pasar al siguiente agente.",
            "#a78bfa",
            "Video scriptwriter",
            "Write hooks, scripts, calls to action and per-platform copy in the brand voice, ready for human review.",
            "You write for short and long-form video. You follow the brand kit, the audience and the goal of the project, "
            "and you never state facts that the research did not provide.",
        ),
        AgentInfo(
            "clipper",
            "Clipper",
            "Selección y recorte de momentos",
            "Analiza los vídeos que subes, sugiere fragmentos con marcas de tiempo y explica por qué cada momento puede funcionar. "
            "Nunca descarga vídeos de terceros.",
            "#f472b6",
            "Video clip selector",
            "Pick the strongest moments of an uploaded video using measured signals and the transcript, and explain each choice.",
            "You only work with files the user uploaded. You justify every clip with real evidence from the analysis tool.",
        ),
        AgentInfo(
            "editor",
            "Editor",
            "Montaje y adaptación de vídeo",
            "Prepara versiones horizontales y verticales con FFmpeg: recorte, encuadre, subtítulos y exportación por plataforma, "
            "con una lista de comprobación antes de exportar.",
            "#38bdf8",
            "Video editor",
            "Render each approved clip in the formats the target platforms need and report the result of every export check.",
            "You edit with FFmpeg through your render tool. You only use footage and audio the user declared rights for.",
        ),
        AgentInfo(
            "designer",
            "Designer",
            "Miniaturas y recursos gráficos",
            "Compone miniaturas y portadas con el kit de marca, manteniendo los textos dentro de las zonas seguras de cada plataforma.",
            "#e879f9",
            "Thumbnail and cover designer",
            "Compose thumbnails and covers with the brand kit, keeping all text inside each platform's safe zone.",
            "You design for small screens: few words, strong contrast, brand colours and typography.",
        ),
        AgentInfo(
            "publisher",
            "Publisher",
            "Programación y publicación",
            "Prepara títulos, descripciones, etiquetas, portada, cuenta y fecha de cada pieza y valida los requisitos de la plataforma. "
            "Solo publica con APIs oficiales y tras tu confirmación.",
            "#fbbf24",
            "Publishing coordinator",
            "Prepare a validated draft for every piece and platform. Never publish: publication needs explicit human confirmation.",
            "You prepare metadata and check platform requirements. Publishing itself is done by the official connectors after a person confirms.",
        ),
        AgentInfo(
            "community",
            "Community",
            "Comentarios y comunidad",
            "Clasifica preguntas, comentarios positivos, spam e incidencias y sugiere respuestas con la voz de la marca. "
            "No envía nada sin tu aprobación.",
            "#34d399",
            "Community manager",
            "Classify comments and draft replies in the brand voice for a human to approve.",
            "You help a creator keep up with their audience. Replies are suggestions; a person always decides what is sent.",
        ),
        AgentInfo(
            "analyst",
            "Analyst",
            "Métricas y recomendaciones",
            "Resume las métricas disponibles, compara resultados cuando hay datos suficientes y propone recomendaciones accionables. "
            "Distingue datos de plataforma, incompletos y manuales.",
            "#818cf8",
            "Performance analyst",
            "Turn the available metrics into actionable recommendations, citing only numbers returned by the statistics tool.",
            "You are careful with small samples: when the data is not enough to compare, you say so instead of guessing.",
        ),
    )
}

AGENT_ORDER = list(AGENTS)


@dataclass(frozen=True)
class StepDef:
    kind: str
    agent: str  # an AGENTS key, or "orchestrator" for the pre-flight review
    title: str
    produces: str
    depends_on: tuple[str, ...] = ()
    requires_approval: bool = True
    # "auto" stages run as soon as their dependencies are met; "data" stages
    # wait for comments or metrics to exist.
    trigger: str = "auto"
    waiting_hint: str = ""


STEPS: tuple[StepDef, ...] = (
    StepDef(
        "research",
        "scout",
        "Investigación y ángulos",
        "Ángulos, hallazgos con fuente y palabras clave",
    ),
    StepDef(
        "script",
        "writer",
        "Guion y textos",
        "Guion, ganchos y textos por plataforma",
        ("research",),
    ),
    StepDef(
        "clips",
        "clipper",
        "Selección de clips",
        "Clips sugeridos con marcas de tiempo",
        ("research",),
    ),
    StepDef(
        "edit",
        "editor",
        "Edición y exportación",
        "Piezas de vídeo por formato",
        ("clips",),
    ),
    StepDef(
        "design",
        "designer",
        "Miniaturas y portadas",
        "Miniaturas y portadas",
        ("script",),
    ),
    StepDef(
        "preflight",
        "orchestrator",
        "Revisión previa",
        "Informe de formato, subtítulos, textos, derechos y metadatos",
        ("script", "edit", "design"),
        requires_approval=False,
    ),
    StepDef(
        "publish",
        "publisher",
        "Programación y publicación",
        "Borradores de publicación validados",
        ("preflight",),
    ),
    StepDef(
        "community",
        "community",
        "Comentarios",
        "Comentarios clasificados y respuestas sugeridas",
        trigger="data",
        waiting_hint="Se activa cuando haya comentarios (sincronizados de una cuenta conectada o importados a mano).",
    ),
    StepDef(
        "analysis",
        "analyst",
        "Analítica",
        "Resumen de resultados y recomendaciones",
        requires_approval=False,
        trigger="data",
        waiting_hint="Se activa cuando haya métricas (de una cuenta conectada o añadidas a mano).",
    ),
)

STEP_BY_KIND = {s.kind: s for s in STEPS}

PLATFORMS: dict[str, dict[str, str]] = {
    "youtube": {"name": "YouTube", "provider": "youtube", "format": "horizontal"},
    "youtube_shorts": {
        "name": "YouTube Shorts",
        "provider": "youtube",
        "format": "vertical",
    },
    "tiktok": {"name": "TikTok", "provider": "tiktok", "format": "vertical"},
    "instagram_reels": {
        "name": "Instagram Reels",
        "provider": "instagram",
        "format": "vertical",
    },
}

FORMATS: dict[str, tuple[int, int]] = {
    "vertical": (1080, 1920),
    "horizontal": (1920, 1080),
    "square": (1080, 1080),
}


@dataclass(frozen=True)
class Template:
    id: str
    name: str
    description: str
    needs_video: bool
    platforms: tuple[str, ...]
    goal: str
    options: dict[str, int] = field(default_factory=dict)
    structure: tuple[str, ...] = (
        "Gancho",
        "Contexto",
        "Desarrollo",
        "Cierre y llamada a la acción",
    )


TEMPLATES: tuple[Template, ...] = (
    Template(
        "repurpose",
        "Vídeo largo → clips verticales",
        "Sube un vídeo y conviértelo en clips para Shorts, Reels y TikTok.",
        True,
        ("youtube_shorts", "tiktok", "instagram_reels"),
        "Dar a conocer el vídeo original con clips cortos",
        {"clip_count": 5, "clip_min_s": 15, "clip_max_s": 60},
    ),
    Template(
        "idea_video",
        "Idea → vídeo de YouTube",
        "Parte de una idea: investigación, guion completo y, cuando grabes, edición y miniatura.",
        False,
        ("youtube",),
        "Explicar el tema con profundidad y ganar suscriptores",
        {"clip_count": 1, "clip_min_s": 60, "clip_max_s": 900},
        (
            "Gancho",
            "Presentación",
            "Punto 1",
            "Punto 2",
            "Punto 3",
            "Resumen",
            "Llamada a la acción",
        ),
    ),
    Template(
        "short_script",
        "Short desde cero",
        "Guion breve para una pieza vertical de menos de un minuto.",
        False,
        ("youtube_shorts", "tiktok", "instagram_reels"),
        "Conseguir alcance con una idea concreta",
        {"clip_count": 1, "clip_min_s": 15, "clip_max_s": 60},
        (
            "Gancho (0-3 s)",
            "Idea central",
            "Prueba o ejemplo",
            "Cierre y llamada a la acción",
        ),
    ),
    Template(
        "tutorial",
        "Tutorial paso a paso",
        "Explica un proceso en pasos claros, con versión larga y resumen vertical.",
        False,
        ("youtube", "youtube_shorts"),
        "Enseñar a hacer algo y demostrar experiencia",
        {"clip_count": 3, "clip_min_s": 20, "clip_max_s": 60},
        (
            "Gancho",
            "Qué vas a aprender",
            "Paso 1",
            "Paso 2",
            "Paso 3",
            "Errores a evitar",
            "Llamada a la acción",
        ),
    ),
    Template(
        "launch",
        "Lanzamiento o anuncio",
        "Campaña en varias plataformas para presentar algo nuevo.",
        False,
        ("youtube", "youtube_shorts", "tiktok", "instagram_reels"),
        "Anunciar una novedad y llevar tráfico a un enlace",
        {"clip_count": 4, "clip_min_s": 15, "clip_max_s": 45},
        (
            "Gancho",
            "Qué es",
            "Para quién es",
            "Cómo conseguirlo",
            "Llamada a la acción",
        ),
    ),
    Template(
        "podcast",
        "Pódcast o directo → momentos destacados",
        "Extrae los mejores momentos de una grabación larga.",
        True,
        ("youtube_shorts", "tiktok", "instagram_reels"),
        "Atraer oyentes al episodio completo",
        {"clip_count": 8, "clip_min_s": 20, "clip_max_s": 90},
    ),
)

TEMPLATE_BY_ID = {t.id: t for t in TEMPLATES}

LANGUAGES = {
    "es": "Español",
    "en": "English",
    "pt": "Português",
    "fr": "Français",
    "de": "Deutsch",
    "it": "Italiano",
}
