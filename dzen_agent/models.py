from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class ArticleStatus(StrEnum):
    DRAFT = "DRAFT"
    READY_FOR_REVIEW = "READY_FOR_REVIEW"
    APPROVED = "APPROVED"
    PREPARED_IN_DZEN = "PREPARED_IN_DZEN"
    RESEARCHED = "RESEARCHED"
    WRITTEN = "WRITTEN"
    IMAGE_READY = "IMAGE_READY"
    PREPARING_IN_DZEN = "PREPARING_IN_DZEN"
    SCHEDULED = "SCHEDULED"
    SCHEDULED_VERIFIED = "SCHEDULED_VERIFIED"
    PUBLISHED = "PUBLISHED"
    ERROR = "ERROR"


@dataclass(frozen=True)
class Article:
    id: int
    topic: str
    title: str
    body: str
    status: ArticleStatus
    created_at: datetime
    updated_at: datetime

