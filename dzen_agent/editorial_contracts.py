"""Author-defined editorial JSON 1.0. Legacy schemas remain in schemas.py."""
from __future__ import annotations

from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, HttpUrl, model_validator

from dzen_agent.analytics_contract import ArticleID, Confidence, Count, Period


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Window(Contract):
    start: str | None
    end: str | None
    timezone: str

    @model_validator(mode="after")
    def valid(self):
        Period(start=self.start, end=self.end, as_of=None, timezone=self.timezone)
        return self


Format = Literal["thematic_selection", "making_of", "film_history", "actor_story", "director_story",
                 "fact_vs_fiction", "adaptation_comparison", "film_comparison", "premiere_preview",
                 "cinema_news", "historical_analysis", "other"]
SourceType = Literal["primary", "official", "industry", "database", "secondary", "other"]
Usage = Literal["direct", "attributed", "context_only", "exclude"]


class InitialSource(Contract):
    source_id: str = Field(min_length=1)
    title: str
    url: HttpUrl
    supports: str


class DiscoverySource(InitialSource):
    publisher: str
    publication_date: str | None
    source_type: SourceType


class AnchorFact(Contract):
    claim: str
    source_ids: list[str]
    confidence: Confidence


class Subject(Contract):
    title: str
    year: int | None
    relevance: str
    source_ids: list[str]


class Selection(Contract):
    status: Literal["not_applicable", "validated", "needs_research"]
    target_count: Count | None
    verified_subjects: list[Subject]


class Scores(Contract):
    audience_fit: int = Field(strict=True, ge=0, le=5)
    channel_novelty: int = Field(strict=True, ge=0, le=5)
    headline_potential: int = Field(strict=True, ge=0, le=5)
    source_strength: int = Field(strict=True, ge=0, le=5)
    editorial_fit: int = Field(strict=True, ge=0, le=5)


def unique(items, key):
    values = [getattr(x, key) for x in items]
    if len(set(values)) != len(values):
        raise ValueError(f"Повторяются {key}.")
    return set(values)


def refs(values, allowed, label):
    if not set(values).issubset(allowed):
        raise ValueError(f"Несуществующая ссылка: {label}.")


class Idea(Contract):
    idea_id: str = Field(min_length=1)
    type: Literal["current_event", "evergreen", "anniversary", "seasonal"]
    viability_status: Literal["ready_for_planning", "needs_validation", "rejected"]
    priority: Confidence
    topic: str = Field(min_length=1)
    cluster: str
    angle: str
    working_headline: str
    audience_hook: str
    reader_promise: str
    why_now: str | None
    event_date: str | None
    timing_note: str
    content_format: Format
    novelty_status: Literal["new", "related_new_angle", "repeat_risk", "unknown"]
    possible_duplicate_article_ids: list[ArticleID]
    analytics_observation_ids: list[str]
    editorial_rationale: str
    scores: Scores
    anchor_facts: list[AnchorFact]
    sources: list[DiscoverySource]
    selection_feasibility: Selection
    research_questions: list[str]
    risks: list[str]
    series_potential: str | None

    @model_validator(mode="after")
    def references(self):
        ids = unique(self.sources, "source_id")
        for fact in self.anchor_facts:
            refs(fact.source_ids, ids, "anchor_facts.source_ids")
        for subject in self.selection_feasibility.verified_subjects:
            refs(subject.source_ids, ids, "verified_subjects.source_ids")
        if self.viability_status == "ready_for_planning":
            if not any(f.source_ids and f.claim.strip() for f in self.anchor_facts):
                raise ValueError("Готовая идея требует подтверждённого факта.")
            selection = self.selection_feasibility
            if self.content_format == "thematic_selection" and (
                selection.status != "validated" or not selection.target_count
                or len(selection.verified_subjects) < selection.target_count
            ):
                raise ValueError("Подборка не проверена в заявленном объёме.")
        return self


class SearchReport(Contract):
    performed: bool
    online_access: Literal["available", "limited", "unavailable"]
    areas_searched: list[str]
    search_queries: list[str]
    limitations: list[str]


class RejectedIdea(Contract):
    topic: str
    reason_code: Literal["duplicate", "insufficient_sources", "weak_angle", "insufficient_subjects",
                         "out_of_scope", "unreliable_claim", "excessive_similarity", "other"]
    reason: str
    related_article_ids: list[ArticleID]


class DiscoveryHandoff(Contract):
    summary: str
    new_clusters: list[str]
    strongest_opportunities: list[str]
    concentration_warnings: list[str]
    unresolved_questions: list[str]


class DiscoveryV1(Contract):
    contract_version: Literal["1.0"]
    stage: Literal["discovery"]
    status: Literal["ok", "partial", "insufficient_data"]
    run_id: ArticleID | None
    planning_window: Window
    search_report: SearchReport
    candidates: list[Idea]
    rejected_ideas: list[RejectedIdea]
    handoff: DiscoveryHandoff

    @model_validator(mode="after")
    def references(self):
        unique(self.candidates, "idea_id")
        return self


class ResearchBrief(Contract):
    objective: str
    questions: list[str]
    claims_to_verify: list[str]
    essential_entities: list[str]
    selection_subjects: list[str]
    initial_source_ids: list[str]
    editorial_constraints: list[str]
    sufficiency_criteria: list[str]


class PlanItem(Contract):
    concept_id: str = Field(min_length=1)
    revision: int = Field(ge=1)
    origin: Literal["new", "existing"]
    approval_status: Literal["pending_review", "approved", "rejected"]
    locked: bool
    source_idea_id: str | None
    scheduled_at: AwareDatetime
    timezone: str
    topic: str = Field(min_length=1)
    cluster: str
    content_format: Format
    working_headline: str
    angle: str
    key_thesis: str
    reader_promise: str
    audience_hook: str
    editorial_rationale: str
    primary_goal: Literal["reach", "retention", "subscription", "audience_expansion", "experiment"]
    strategy_role: Literal["proven", "adjacent", "experimental", "timely"]
    analytics_observation_ids: list[str]
    analytics_experiment_ids: list[str]
    timing_rationale: str
    initial_sources: list[InitialSource]
    research_brief: ResearchBrief
    risks: list[str]
    editorial_notes: str

    @model_validator(mode="after")
    def references(self):
        ids = unique(self.initial_sources, "source_id")
        refs(self.research_brief.initial_source_ids, ids, "research_brief.initial_source_ids")
        if self.source_idea_id and not self.initial_sources:
            raise ValueError("Концепт из Discovery должен сохранять первоначальные источники.")
        return self


class Target(Contract):
    article_count: Count
    target_scope: Literal["total_period", "new_only"]
    selected_count: Count
    existing_count: Count
    unfilled_count: Count


class Strategy(Contract):
    period_goal: str
    main_approach: str
    proven_directions: list[str]
    expansion_directions: list[str]
    experimental_directions: list[str]
    limitations: list[str]


class Alternate(Contract):
    source_idea_id: str
    working_headline: str
    reason_to_keep: str
    possible_replacement_for: list[str]
    limitations: list[str]


class RejectedCandidate(Contract):
    source_idea_id: str
    reason_code: Literal["duplicate", "weak_angle", "insufficient_sources", "low_priority", "thematic_saturation",
                         "scheduling_conflict", "insufficient_selection", "out_of_scope", "other"]
    reason: str


class Unfilled(Contract):
    reason_code: Literal["insufficient_candidates", "insufficient_sources", "scheduling_conflict",
                         "editorial_restriction", "missing_data", "other"]
    count: Count
    details: str
    suggested_discovery_direction: str


class PlanHandoff(Contract):
    approval_required: bool
    ready_for_editorial_review: list[str]
    discovery_followup: list[str]
    production_blockers: list[str]


class PlannerV1(Contract):
    contract_version: Literal["1.0"]
    stage: Literal["planner"]
    status: Literal["ok", "partial", "insufficient_data"]
    run_id: ArticleID | None
    plan_id: ArticleID | None
    plan_revision: int = Field(ge=1)
    mode: Literal["create", "revise"]
    period: Window
    target: Target
    editorial_strategy: Strategy
    items: list[PlanItem]
    alternates: list[Alternate]
    rejected_candidates: list[RejectedCandidate]
    unfilled_slots: list[Unfilled]
    editorial_warnings: list[str]
    handoff: PlanHandoff

    @model_validator(mode="after")
    def references(self):
        ids = unique(self.items, "concept_id")
        refs(self.handoff.ready_for_editorial_review, ids, "ready_for_editorial_review")
        selected = sum(i.origin == "new" for i in self.items)
        existing = len(self.items) - selected
        unfilled = max(0, self.target.article_count - selected - (
            existing if self.target.target_scope == "total_period" else 0))
        if (selected, existing, unfilled) != (self.target.selected_count, self.target.existing_count, self.target.unfilled_count):
            raise ValueError("Некорректные счётчики плана.")
        if sum(x.count for x in self.unfilled_slots) != unfilled:
            raise ValueError("Не описаны незаполненные места плана.")
        if self.status == "ok" and unfilled:
            raise ValueError("Неполный план не может иметь status=ok.")
        return self


class Work(Contract):
    work_id: str
    title: str
    year: int | None
    work_type: Literal["film", "series", "other"]
    director_names: list[str]
    lead_cast: list[str]
    production_notes: list[str]
    relevance_to_article: str
    source_ids: list[str]
    fact_ids: list[str]


class Person(Contract):
    person_id: str
    full_name: str
    role_in_topic: str
    related_work_ids: list[str]
    source_ids: list[str]
    notes: str


class Fact(Contract):
    fact_id: str
    claim: str
    work_ids: list[str]
    person_ids: list[str]
    source_ids: list[str]
    evidence_type: Literal["direct", "attributed", "inference"]
    confidence: Confidence
    usage: Usage
    importance: Literal["core", "supporting", "background"]
    context: str
    qualification: str | None


class Quote(Contract):
    quote_id: str
    speaker: str
    exact_text: str
    original_language: str
    source_ids: list[str]
    context: str
    relevance: str
    verification_status: Literal["verified", "partially_verified", "unverified"]
    usage: Usage
    translation: str | None


class Timeline(Contract):
    date: str | None
    year: int | None
    date_precision: Literal["day", "month", "year", "approximate", "unknown"]
    event: str
    fact_ids: list[str]
    source_ids: list[str]


class ContradictionVersion(Contract):
    description: str
    source_ids: list[str]


class Contradiction(Contract):
    contradiction_id: str
    subject: str
    versions: list[ContradictionVersion]
    resolution_status: str
    assessment: str
    writer_instruction: str


class ExcludedClaim(Contract):
    claim_id: str
    claim: str
    reason: str
    source_ids: list[str]
    risk_level: Confidence


class ResearchSource(Contract):
    source_id: str
    title: str
    publisher: str
    author: str | None
    url: HttpUrl
    publication_date: str | None
    source_type: SourceType
    retrieval_status: Literal["full_text", "search_snippet", "metadata_only", "unavailable"]
    independence_group: str
    relevant_location: str | None
    supports: list[str]
    reliability_note: str


class Section(Contract):
    section_id: str
    section_purpose: str
    main_point: str
    fact_ids: list[str]
    quote_ids: list[str]
    work_ids: list[str]
    transition_note: str


class Sufficiency(Contract):
    target_length_chars: Count | None
    assessment: str
    coverage: Literal["sufficient", "limited", "insufficient"]
    missing_sections: list[str]


class WriterHandoff(Contract):
    approved_fact_ids: list[str]
    approved_quote_ids: list[str]
    forbidden_claim_ids: list[str]
    mandatory_points: list[str]
    cautions: list[str]
    sufficiency: Sufficiency


class ConceptValidation(Contract):
    original_thesis: str
    thesis_status: str
    assessment: str
    proposed_change: str | None


class ResearchSummary(Contract):
    topic: str
    angle: str
    main_findings: list[str]
    most_valuable_details: list[str]
    editorial_opportunities: list[str]


class ResearchV1(Contract):
    contract_version: Literal["1.0"]
    stage: Literal["research"]
    status: Literal["ok", "partial", "insufficient_data", "replan_required", "blocked"]
    run_id: ArticleID | None
    concept_id: str
    concept_revision: int = Field(ge=1)
    research_status: Literal["ready", "needs_more_research", "editorial_review_required", "blocked"]
    concept_validation: ConceptValidation
    research_summary: ResearchSummary
    works: list[Work]
    people: list[Person]
    facts: list[Fact]
    quotes: list[Quote]
    timeline: list[Timeline]
    contradictions: list[Contradiction]
    excluded_claims: list[ExcludedClaim]
    sources: list[ResearchSource]
    article_structure: list[Section]
    writer_handoff: WriterHandoff
    research_limitations: list[str]
    followup_questions: list[str]

    @model_validator(mode="after")
    def references(self):
        groups = {key: unique(getattr(self, key), id_key) for key, id_key in (
            ("works", "work_id"), ("people", "person_id"), ("facts", "fact_id"), ("quotes", "quote_id"),
            ("excluded_claims", "claim_id"), ("sources", "source_id"), ("article_structure", "section_id"),
            ("contradictions", "contradiction_id"))}
        for collection in (self.works, self.people, self.facts, self.quotes, self.timeline, self.excluded_claims):
            for item in collection:
                refs(item.source_ids, groups["sources"], "source_ids")
        for item in self.works + self.article_structure + self.timeline:
            refs(item.fact_ids, groups["facts"], "fact_ids")
        for item in self.people:
            refs(item.related_work_ids, groups["works"], "related_work_ids")
        for item in self.facts:
            refs(item.work_ids, groups["works"], "work_ids")
            refs(item.person_ids, groups["people"], "person_ids")
        for item in self.article_structure:
            refs(item.quote_ids, groups["quotes"], "quote_ids")
            refs(item.work_ids, groups["works"], "work_ids")
        for contradiction in self.contradictions:
            for version in contradiction.versions:
                refs(version.source_ids, groups["sources"], "versions.source_ids")
        for source in self.sources:
            refs(source.supports, groups["facts"] | groups["quotes"], "supports")
        handoff = self.writer_handoff
        refs(handoff.approved_fact_ids, {f.fact_id for f in self.facts if f.usage in ("direct", "attributed") and f.source_ids},
             "approved_fact_ids")
        refs(handoff.approved_quote_ids, {q.quote_id for q in self.quotes if q.usage in ("direct", "attributed")
                                        and q.verification_status == "verified" and q.source_ids}, "approved_quote_ids")
        refs(handoff.forbidden_claim_ids, groups["excluded_claims"], "forbidden_claim_ids")
        if self.research_status == "ready" and (self.status not in ("ok", "partial")
                or handoff.sufficiency.coverage != "sufficient" or not handoff.approved_fact_ids):
            raise ValueError("Research не готов к Writer.")
        return self


class ArticleContent(Contract):
    headline: str
    article_markdown: str
    character_count: Count | None
    comments_question: str


class Evidence(Contract):
    fact_id: str
    anchor_text: str = Field(min_length=1)


class QuoteUsed(Contract):
    quote_id: str
    anchor_text: str = Field(min_length=1)


class WriterIssue(Contract):
    code: Literal["insufficient_facts", "missing_work_metadata", "contradictory_sources", "headline_not_supported",
                  "length_problem", "concept_mismatch", "research_mismatch", "editorial_decision_required", "other"]
    severity: Confidence
    description: str
    related_fact_ids: list[str]


class QAHandoff(Contract):
    ready_for_qa: bool
    needs_research: list[str]
    needs_editorial_decision: list[str]


class WriterV1(Contract):
    contract_version: Literal["1.0"]
    stage: Literal["writer"]
    status: Literal["ok", "partial", "blocked", "replan_required"]
    run_id: ArticleID | None
    concept_id: str
    concept_revision: int = Field(ge=1)
    content: ArticleContent
    evidence_map: list[Evidence]
    quotes_used: list[QuoteUsed]
    issues: list[WriterIssue]
    handoff: QAHandoff

    @model_validator(mode="after")
    def readiness(self):
        if self.status == "ok" and (not self.handoff.ready_for_qa or not self.content.headline.strip()
                                     or not self.content.article_markdown.strip()):
            raise ValueError("Writer status=ok требует готовую статью.")
        if self.status != "ok" and self.handoff.ready_for_qa:
            raise ValueError("Неготовая статья не передаётся в QA.")
        if self.status in ("blocked", "replan_required") and self.content.article_markdown:
            raise ValueError("Заблокированный Writer не должен создавать статью.")
        return self

    def validate_research(self, research: ResearchV1):
        if (self.concept_id, self.concept_revision) != (research.concept_id, research.concept_revision):
            raise ValueError("Writer изменил идентификатор или версию концепта.")
        for evidence in self.evidence_map:
            refs([evidence.fact_id], research.writer_handoff.approved_fact_ids, "evidence_map")
            if evidence.anchor_text not in self.content.article_markdown:
                raise ValueError("Evidence anchor отсутствует в статье.")
        for quote in self.quotes_used:
            refs([quote.quote_id], research.writer_handoff.approved_quote_ids, "quotes_used")
            original = next(q for q in research.quotes if q.quote_id == quote.quote_id)
            if quote.anchor_text not in self.content.article_markdown or not any(
                    text and text in quote.anchor_text for text in (original.exact_text, original.translation)):
                raise ValueError("Цитата Writer не совпадает с Research.")
        for issue in self.issues:
            refs(issue.related_fact_ids, {f.fact_id for f in research.facts}, "related_fact_ids")


class VisualPlan(Contract):
    expected_inline_images: Count
    selected_inline_images: Count
    missing_sections: list[str]


class Cover(Contract):
    status: Literal["ready", "unavailable", "failed", "not_attempted"]
    source_asset_id: str | None
    cover_asset_id: str | None
    cover_text: str
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    aspect_ratio: Literal["16:9"]
    layout_description: str
    validation_issues: list[str]


class Asset(Contract):
    asset_id: str = Field(min_length=1)
    media_type: Literal["film_still", "series_still", "behind_the_scenes", "actor_photo", "director_photo",
                        "official_poster", "archive_photo", "other"]
    related_work_id: str | None
    related_section_id: str | None
    source_page_url: HttpUrl
    source_image_url: HttpUrl | None
    source_name: str
    attribution: str | None
    rights_status: Literal["permitted", "requires_review", "restricted"]
    authenticity_status: Literal["verified", "probable", "uncertain", "rejected"]
    authenticity_basis: str
    local_file_ref: str | None
    file_hash: str | None
    width: Count | None
    height: Count | None
    download_status: Literal["not_attempted", "downloaded", "failed", "unavailable"]
    selection_rationale: str
    issues: list[str]


class Placement(Contract):
    placement_id: str
    asset_id: str
    role: Literal["cover", "inline"]
    target_section_heading: str
    anchor_text: str
    occurrence_index: Count
    placement_rule: Literal["before_intro", "after_cast_block", "after_heading", "after_paragraph", "other"]
    visible_caption: str | None
    alt_text: str
    status: str


class Assembly(Contract):
    status: Literal["ready", "partial", "blocked", "failed"]
    headline: str
    original_content_hash: str | None
    assembled_document_ref: str | None
    assembly_version: int = Field(ge=1)
    visible_captions: bool
    cover_in_body: bool
    validation_issues: list[str]


class PublishingHandoff(Contract):
    requested_action: str
    execution_status: Literal["not_attempted", "pending", "succeeded", "failed", "unknown"]
    draft_id: str | None
    draft_url: str | None
    verified_in_dzen: bool
    issues: list[str]


class ImageV1(Contract):
    contract_version: Literal["1.0"]
    stage: Literal["image"]
    status: Literal["ok", "partial", "blocked", "failed"]
    run_id: ArticleID | None
    concept_id: str
    concept_revision: int = Field(ge=1)
    visual_plan: VisualPlan
    cover: Cover
    assets: list[Asset]
    placements: list[Placement]
    assembly: Assembly
    publishing_handoff: PublishingHandoff
    issues: list[str]

    @model_validator(mode="after")
    def references(self):
        ids = unique(self.assets, "asset_id")
        unique(self.placements, "placement_id")
        for placement in self.placements:
            refs([placement.asset_id], ids, "placements.asset_id")
            if placement.visible_caption is not None:
                raise ValueError("Подписи под изображениями отключены.")
        if self.cover.source_asset_id:
            refs([self.cover.source_asset_id], ids, "cover.source_asset_id")
        return self
