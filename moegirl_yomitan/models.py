from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass
class ManifestPage:
    source_url: str
    title_from_url: str
    lastmod: str
    sitemap_url: str
    pageid: int | None = None
    canonical_title: str | None = None
    article_url: str | None = None
    record_path: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ManifestPage":
        return cls(**data)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SummaryRecord:
    pageid: int
    canonical_title: str
    article_url: str
    source_url: str
    lastmod: str
    summary: str
    retrieved_at: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SummaryRecord":
        return cls(**data)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
