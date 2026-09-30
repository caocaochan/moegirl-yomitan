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
    fetched_lastmod: str | None = None

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
    summary_char_limit: int | None = None
    summary_complete: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SummaryRecord":
        if not isinstance(data, dict):
            raise ValueError("record must be an object")
        if type(data.get("pageid")) is not int or data["pageid"] <= 0:
            raise ValueError("record pageid must be a positive integer")
        for name in ("canonical_title", "article_url", "source_url", "lastmod", "summary", "retrieved_at"):
            if not isinstance(data.get(name), str):
                raise ValueError(f"record {name} must be a string")
        if not data["canonical_title"] or not data["summary"]:
            raise ValueError("record title and summary must not be empty")
        if type(data.get("schema_version", 1)) is not int or data.get("schema_version", 1) <= 0:
            raise ValueError("record schema_version must be a positive integer")
        limit = data.get("summary_char_limit")
        if limit is not None and (type(limit) is not int or limit <= 0):
            raise ValueError("record summary_char_limit must be a positive integer")
        if type(data.get("summary_complete", False)) is not bool:
            raise ValueError("record summary_complete must be a boolean")
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
            summary_char_limit=limit,
            summary_complete=data.get("summary_complete", False),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
