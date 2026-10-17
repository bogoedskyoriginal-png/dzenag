from __future__ import annotations

from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, HttpUrl, model_validator


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


class EditorialProposal(Output):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    type: Literal["replace_concept", "update_concept", "reschedule_concept"]
    concept_id: int = Field(gt=0)
    reason: str = Field(min_length=1, max_length=2000)
    new_topic: str | None = Field(default=None, min_length=1, max_length=600)
    working_title: str | None = Field(default=None, min_length=1, max_length=600)
    angle: str | None = Field(default=None, min_length=1, max_length=2000)
    why_now: str | None = Field(default=None, min_length=1, max_length=2000)
    audience_value: str | None = Field(default=None, min_length=1, max_length=2000)
    thesis: str | None = Field(default=None, min_length=1, max_length=2000)
    key_points: list[str] | None = Field(default=None, min_length=1, max_length=12)
    scheduled_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def check_action(self):
        text_changes = any(getattr(self, key) is not None for key in (
            "new_topic", "working_title", "angle", "why_now", "audience_value", "thesis", "key_points"))
        if self.type == "reschedule_concept":
            if not self.scheduled_at or text_changes:
                raise ValueError("Перенос содержит только дату, concept_id и причину.")
        elif self.scheduled_at is not None or not text_changes:
            raise ValueError("Укажите текстовые изменения; перенос даты — отдельное предложение.")
        if self.type == "replace_concept" and not all((self.new_topic, self.working_title, self.angle)):
            raise ValueError("Для замены нужны тема, рабочий заголовок и ракурс.")
        if self.key_points and any(not p.strip() or len(p) > 1000 for p in self.key_points):
            raise ValueError("Ключевые пункты должны быть непустыми и краткими.")
        return self


class EditorialReply(Output):
    message: str = Field(min_length=1, max_length=12000)
    proposals: list[EditorialProposal] = Field(default_factory=list, max_length=10)
