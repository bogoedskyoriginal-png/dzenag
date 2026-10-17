from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl


class Output(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Analytics(Output):
    summary: str = Field(min_length=1)
    best_topics: list[str]
    weak_topics: list[str]
    useful_patterns: list[str]
    headline_patterns: list[str]
    recommendations: list[str]
    topics_to_avoid: list[str]
    opportunities: list[str]


class Source(Output):
    id: str = Field(min_length=1)
    url: HttpUrl
    title: str
    publication_date: str | None = None
    excerpt: str = ""


class Claim(Output):
    text: str = Field(min_length=1)
    source_ids: list[str] = Field(min_length=1)


class Research(Output):
    status: Literal["sufficient", "insufficient_research"]
    summary: str
    key_facts: list[str]
    claims: list[Claim]
    sources: list[Source]
    contradictions: list[str]
    uncertainty: list[str]
    suggested_angles: list[str]


class Concept(Output):
    topic: str = Field(min_length=1)
    working_title: str = Field(min_length=1)
    angle: str
    why_now: str
    audience_value: str
    thesis: str
    key_points: list[str]
    research_job_id: int
    research_source_ids: list[str] = Field(min_length=1)
    suggested_publication_date: str
    suggested_publication_time: str
    score: float = Field(ge=0, le=100)


class Plan(Output):
    concepts: list[Concept] = Field(min_length=1)


class WrittenArticle(Output):
    title: str = Field(min_length=1)
    body: str = Field(min_length=1)
    short_summary: str = Field(min_length=1)
    source_ids: list[str] = Field(min_length=1)


class ContentQA(Output):
    passed: bool
    issues: list[str]


class NewTitle(Output):
    title: str = Field(min_length=1)


class NewBody(Output):
    body: str = Field(min_length=1)
    short_summary: str = Field(min_length=1)
    source_ids: list[str] = Field(min_length=1)
