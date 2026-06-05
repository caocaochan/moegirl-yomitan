from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


SUMMARY_RECORD_SCHEMA_VERSION = 2


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
class ListedLink:
    title: str
    url: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ListedLink" | None:
        title = data.get("title")
        url = data.get("url")
        if not isinstance(title, str) or not isinstance(url, str):
            return None
        if not title or not url:
            return None
        return cls(title=title, url=url)


@dataclass
class SummaryRecord:
    pageid: int
    canonical_title: str
    article_url: str
    source_url: str
    lastmod: str
    summary: str
    retrieved_at: str
    listed_links: list[ListedLink] = field(default_factory=list)
    schema_version: int = SUMMARY_RECORD_SCHEMA_VERSION

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SummaryRecord":
        listed_links = []
        raw_listed_links = data.get("listed_links", [])
        if isinstance(raw_listed_links, list):
            for raw_link in raw_listed_links:
                if not isinstance(raw_link, dict):
                    continue
                link = ListedLink.from_dict(raw_link)
                if link is not None:
                    listed_links.append(link)

        return cls(
            pageid=data["pageid"],
            canonical_title=data["canonical_title"],
            article_url=data["article_url"],
            source_url=data["source_url"],
            lastmod=data["lastmod"],
            summary=data["summary"],
            retrieved_at=data["retrieved_at"],
            listed_links=listed_links,
            schema_version=data.get("schema_version", 1),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
