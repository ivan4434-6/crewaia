"""Structured outputs each agent must return (``Task.output_pydantic``)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Confidence = Literal["alta", "media", "baja"]


class Finding(BaseModel):
    claim: str = Field(
        description="What the source says, in one or two sentences. Quote or paraphrase closely."
    )
    source_title: str = Field(
        description="Title of the page or item, as returned by the tool."
    )
    url: str = Field(description="Exact URL returned by the fetch_sources tool.")
    published: str | None = Field(
        default=None,
        description="Publication date from the tool result (ISO), or null.",
    )
    confidence: Confidence = Field(description="alta | media | baja")
    confidence_reason: str = Field(description="Why this confidence level.")


class Angle(BaseModel):
    title: str
    rationale: str = Field(description="Why this angle fits the audience and goal.")
    audience: str = ""
    keywords: list[str] = Field(default_factory=list)
    source_urls: list[str] = Field(
        default_factory=list,
        description="URLs of findings that support this angle. Empty if none.",
    )


class ResearchOutput(BaseModel):
    angles: list[Angle] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    unverified: list[str] = Field(
        default_factory=list,
        description="Anything that could not be verified with the sources, stated plainly.",
    )
    notes: str = ""


class ScriptSection(BaseModel):
    heading: str
    text: str = Field(description="What is said, written to be spoken aloud.")
    visual: str = Field(default="", description="On-screen text or shot suggestion.")


class PlatformCopy(BaseModel):
    platform: str = Field(
        description="youtube | youtube_shorts | tiktok | instagram_reels"
    )
    title: str = ""
    description: str = ""
    hashtags: list[str] = Field(default_factory=list)


class ScriptOutput(BaseModel):
    working_title: str
    hooks: list[str] = Field(default_factory=list)
    outline: list[str] = Field(default_factory=list)
    sections: list[ScriptSection] = Field(default_factory=list)
    cta: str = ""
    titles: list[str] = Field(default_factory=list)
    platform_copy: list[PlatformCopy] = Field(default_factory=list)
    notes: str = ""


class ClipPick(BaseModel):
    candidate_id: str = Field(
        description="Id of a candidate returned by the analyze_video tool."
    )
    title: str = Field(description="Short working title for the clip.")
    reason: str = Field(
        description="Why this moment can work, based on the measured signals and the transcript."
    )


class ClipsOutput(BaseModel):
    picks: list[ClipPick] = Field(default_factory=list)
    notes: str = ""


class RenderPiece(BaseModel):
    clip_id: str
    format: Literal["vertical", "horizontal", "square"]
    framing: Literal["crop", "blur", "pad"] = "crop"
    subtitles: bool = False


class EditOutput(BaseModel):
    summary: str = ""
    notes: str = ""


class DesignItem(BaseModel):
    kind: Literal["thumbnail", "cover"]
    headline: str = Field(description="Few words, readable on a phone.")
    layout: Literal["side", "band", "center"] = "side"
    background: Literal["frame", "gradient"] = "frame"


class DesignOutput(BaseModel):
    summary: str = ""
    notes: str = ""


class DraftItem(BaseModel):
    publication_id: str
    title: str = ""
    description: str = ""
    tags: list[str] = Field(default_factory=list)


class PublishOutput(BaseModel):
    summary: str = ""
    notes: str = ""


class CommentTriage(BaseModel):
    comment_id: str
    category: Literal["question", "positive", "spam", "issue", "other"]
    reason: str = ""
    reply: str = Field(
        default="", description="Suggested reply in the brand voice. Empty for spam."
    )


class CommunityOutput(BaseModel):
    items: list[CommentTriage] = Field(default_factory=list)
    notes: str = ""


class RecommendationItem(BaseModel):
    text: str = Field(description="One actionable recommendation.")
    evidence: list[str] = Field(
        default_factory=list, description="Figures from compute_stats that support it."
    )


class AnalysisOutput(BaseModel):
    summary: str = ""
    recommendations: list[RecommendationItem] = Field(default_factory=list)
    data_gaps: list[str] = Field(default_factory=list)
