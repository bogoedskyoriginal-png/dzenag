"""Analytics 1.0 and explicit read compatibility with the original V0 result."""
from __future__ import annotations

import math
from datetime import date, datetime, time
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, model_validator

Count = Annotated[int, Field(strict=True, ge=0)]
Number = Annotated[float, Field(strict=True, allow_inf_nan=False)]
Rate = Annotated[float, Field(strict=True, ge=0, le=1, allow_inf_nan=False)]
Confidence = Literal["high", "medium", "low"]
ArticleID = Annotated[str, Field(strict=True)] | Annotated[int, Field(strict=True)]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LegacyAnalytics(Contract):
    summary: str = Field(min_length=1)
    best_topics: list[str]
    weak_topics: list[str]
    useful_patterns: list[str]
    headline_patterns: list[str]
    recommendations: list[str]
    topics_to_avoid: list[str]
    opportunities: list[str]


def timestamp(value: str | date | datetime | None, timezone: str):
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if isinstance(value, datetime):
        return value.replace(tzinfo=ZoneInfo(timezone)) if value.tzinfo is None else value
    return datetime.combine(value, time(), ZoneInfo(timezone))


class Period(Contract):
    start: str | None
    end: str | None
    as_of: str | None
    timezone: str

    @model_validator(mode="after")
    def dates(self):
        try:
            ZoneInfo(self.timezone)
            start, end = timestamp(self.start, self.timezone), timestamp(self.end, self.timezone)
            timestamp(self.as_of, self.timezone)
            if start and end and start > end:
                raise ValueError("Начало периода позже окончания.")
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise ValueError("Некорректный период или часовой пояс Analytics.") from error
        return self


class DataQuality(Contract):
    articles_total: Count
    articles_usable: Count
    issues: list[str]
    missing_metrics: list[str]
    comparison_limitations: list[str]

    @model_validator(mode="after")
    def counts(self):
        if self.articles_usable > self.articles_total:
            raise ValueError("Пригодных публикаций больше, чем всего публикаций.")
        return self


class ChannelDiagnosis(Contract):
    trend: Literal["growing", "stable", "declining", "mixed", "unknown"]
    main_bottleneck: Literal["distribution", "packaging", "retention", "subscription", "mixed", "unknown"]
    summary: str


class Funnel(Contract):
    impressions: Count | None
    opens: Count | None
    completions: Count | None
    subscriptions: Count | None
    ctr: Rate | None
    completion_rate: Rate | None
    completion_per_impression: Rate | None
    subscriptions_per_1000_opens: Annotated[Number, Field(ge=0)] | None

    @model_validator(mode="after")
    def ratios(self):
        for field, numerator, denominator, multiplier in (
            ("ctr", self.opens, self.impressions, 1),
            ("completion_rate", self.completions, self.opens, 1),
            ("completion_per_impression", self.completions, self.impressions, 1),
            ("subscriptions_per_1000_opens", self.subscriptions, self.opens, 1000),
        ):
            actual = getattr(self, field)
            expected = numerator / denominator * multiplier if numerator is not None and denominator else None
            if actual is not None and (expected is None or not math.isclose(actual, expected, rel_tol=1e-5, abs_tol=1e-6)):
                raise ValueError(f"Некорректное отношение {field}; отсутствующий знаменатель не заменяется нулём.")
        return self


class Observation(Contract):
    id: str = Field(min_length=1)
    finding: str
    metric: str
    value: Number | None
    comparison_value: Number | None
    article_ids: list[ArticleID]
    sample_articles: Count
    sample_impressions: Count | None
    sample_opens: Count | None
    confidence: Confidence
    limitations: str

    @model_validator(mode="after")
    def consistent_sample_and_rates(self):
        if len(set(self.article_ids)) > self.sample_articles:
            raise ValueError("В наблюдении перечислено больше статей, чем в выборке.")
        if self.metric.lower() in {"ctr", "completion_rate", "completion_per_impression"}:
            if any(value is not None and not 0 <= value <= 1 for value in (self.value, self.comparison_value)):
                raise ValueError("Отношения возвращаются долями от 0 до 1, а не процентами.")
        return self


class Interpretation(Contract):
    id: str = Field(min_length=1)
    observation_ids: list[str]
    interpretation: str
    alternative_explanations: list[str]
    confidence: Confidence


class ClusterSignal(Contract):
    cluster: str
    direction: Literal["scale", "test", "improve", "reduce", "hold"]
    article_count: Count
    evidence_ids: list[str]
    confidence: Confidence
    reason: str


class Experiment(Contract):
    id: str = Field(min_length=1)
    hypothesis: str
    change: str
    comparison: str
    primary_metric: str
    guardrail_metrics: list[str]
    evaluation_conditions: str
    priority: Confidence
    evidence_ids: list[str]


class DiscoveryBrief(Contract):
    promising_directions: list[str]
    underexplored_directions: list[str]
    avoid_repetition: list[str]
    research_questions: list[str]


class PlannerBrief(Contract):
    content_mix_guidance: str
    priority_clusters: list[str]
    caution_clusters: list[str]
    headline_insights: list[str]
    format_insights: list[str]
    priority_experiment_ids: list[str]


class Analytics(Contract):
    contract_version: Literal["1.0"]
    stage: Literal["analytics"]
    status: Literal["ok", "partial", "insufficient_data"]
    period: Period
    data_quality: DataQuality
    channel_diagnosis: ChannelDiagnosis
    funnel: Funnel
    observations: list[Observation]
    interpretations: list[Interpretation]
    cluster_signals: list[ClusterSignal]
    experiments: list[Experiment]
    discovery_brief: DiscoveryBrief
    planner_brief: PlannerBrief
    missing_data_requests: list[str]

    @model_validator(mode="after")
    def references(self):
        def unique(items):
            ids = {item.id for item in items}
            if len(ids) != len(items):
                raise ValueError("Повторяются идентификаторы Analytics.")
            return ids
        observations = unique(self.observations)
        unique(self.interpretations)
        experiments = unique(self.experiments)
        references = [item.observation_ids for item in self.interpretations]
        references += [item.evidence_ids for item in self.cluster_signals + self.experiments]
        if any(not set(ids).issubset(observations) for ids in references):
            raise ValueError("Ссылка на несуществующее наблюдение Analytics.")
        if not set(self.planner_brief.priority_experiment_ids).issubset(experiments):
            raise ValueError("Planner brief ссылается на несуществующий эксперимент.")
        if any(o.sample_articles > self.data_quality.articles_total for o in self.observations):
            raise ValueError("Размер выборки наблюдения превышает количество публикаций.")
        if any(c.article_count > self.data_quality.articles_total for c in self.cluster_signals):
            raise ValueError("Размер кластера превышает количество публикаций.")
        return self

    def validate_input(self, context: dict) -> None:
        articles = context["articles"]
        ids = {a["article_id"] for a in articles}
        if self.data_quality.articles_total != len(ids):
            raise ValueError("Analytics изменил количество публикаций входной выборки.")
        if any(not set(o.article_ids).issubset(ids) for o in self.observations):
            raise ValueError("Analytics ссылается на публикацию, которой нет во входных данных.")
        if not context["analysis_config"]["comparison_available"] and self.channel_diagnosis.trend != "unknown":
            raise ValueError("Нельзя определять тренд без сопоставимого предыдущего периода.")
        expected_period = context["run_context"]["period"]
        if self.period.timezone != expected_period["timezone"] or any(
                timestamp(getattr(self.period, key), self.period.timezone) != timestamp(expected_period[key], self.period.timezone)
                for key in ("start", "end", "as_of")):
            raise ValueError("Analytics изменил или придумал период измерения.")
        if not articles and self.status != "insufficient_data":
            raise ValueError("Пустая выборка требует status=insufficient_data.")
        expected = context["analysis_config"]["validated_funnel"]
        for key, value in self.funnel.model_dump().items():
            if value is not None and (expected.get(key) is None or not math.isclose(value, expected[key], rel_tol=1e-5, abs_tol=1e-6)):
                raise ValueError(f"Метрика {key} не подтверждена сопоставимыми входными данными.")


def parse_analytics(payload: dict) -> Analytics | LegacyAnalytics:
    # A malformed/unknown version never falls back to the permissive legacy path.
    return Analytics.model_validate(payload) if "contract_version" in payload else LegacyAnalytics.model_validate(payload)


def briefs(payload: dict) -> tuple[dict, dict]:
    result = parse_analytics(payload)
    if isinstance(result, Analytics):
        return result.discovery_brief.model_dump(), result.planner_brief.model_dump()
    # A projection for consumers, not a fabricated Analytics 1.0 result or DB rewrite.
    return ({"promising_directions": result.opportunities + result.best_topics,
             "underexplored_directions": [], "avoid_repetition": result.topics_to_avoid,
             "research_questions": []},
            {"content_mix_guidance": "\n".join(result.recommendations), "priority_clusters": result.best_topics,
             "caution_clusters": result.weak_topics, "headline_insights": result.headline_patterns,
             "format_insights": result.useful_patterns, "priority_experiment_ids": []})


def analytics_summary(payload: dict) -> str:
    result = parse_analytics(payload)
    return result.channel_diagnosis.summary if isinstance(result, Analytics) else result.summary
