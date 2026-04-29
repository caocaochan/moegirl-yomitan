from __future__ import annotations

import json
from pathlib import Path
import re
from zipfile import ZIP_DEFLATED, ZipFile

from pypinyin import Style, lazy_pinyin

from .config import Settings
from .fetcher import load_manifest, load_record, record_path_for_page
from .models import SummaryRecord
from .versioning import resolve_build_version

FULLWIDTH_ALIAS_PATTERN = re.compile(r"^(?P<base>.+?)（[^（）]+）$")
HANZI_RUN_PATTERN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")
READING_COLLAPSE_SPACE_PATTERN = re.compile(r"\s+")
READING_SPACE_BEFORE_PUNCTUATION_PATTERN = re.compile(r"\s+([,.:;!?%)\]}>:：；，。！？、）】》」』])")
READING_SPACE_BEFORE_OPENING_PATTERN = re.compile(r"\s+([(\[<{（【《「『])")
READING_SPACE_AFTER_OPENING_PATTERN = re.compile(r"([(\[<{（【《「『])\s+")
READING_PUNCTUATION_WITH_TRAILING_SPACE_PATTERN = re.compile(r"([,.:;!?:：；，。！？、])(?=\S)")


def package_dictionary(settings: Settings) -> Path:
    manifest_pages = load_manifest(settings)
    deduped: dict[int, SummaryRecord] = {}
    for page in manifest_pages:
        if page.pageid is None:
            continue
        record = load_record(record_path_for_page(settings, page.pageid))
        if record is None or record.lastmod != page.lastmod:
            continue
        existing = deduped.get(record.pageid)
        if existing is None or (record.canonical_title, record.pageid) < (existing.canonical_title, existing.pageid):
            deduped[record.pageid] = record

    ordered_records = sorted(deduped.values(), key=lambda item: (item.canonical_title.casefold(), item.pageid))

    settings.output_zip.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(settings.output_zip, "w", compression=ZIP_DEFLATED, compresslevel=9) as archive:
        archive.writestr("index.json", json.dumps(build_index(settings), ensure_ascii=False, indent=2))
        for file_number, chunk in enumerate(chunked(ordered_records, settings.chunk_size), start=1):
            entries = [entry for record in chunk for entry in build_term_entries(record)]
            archive.writestr(
                f"term_bank_{file_number}.json",
                json.dumps(entries, ensure_ascii=False, separators=(",", ":")),
            )
    return settings.output_zip


def build_index(settings: Settings, revision: str | None = None) -> dict:
    build_version = resolve_build_version() if revision is None else revision
    return {
        "title": settings.dictionary_title,
        "format": 3,
        "revision": build_version,
        "sequenced": True,
        "sourceLanguage": "zh",
        "targetLanguage": "zh",
        "url": "https://mzh.moegirl.org.cn/",
        "description": "来自萌娘百科的简短导语摘要词典。仅包含摘要与原文链接，不提供全文。",
        "attribution": "内容来源：萌娘百科（Moegirlpedia）。本词典仅提供摘要，非全文，请通过词条链接访问原文。",
    }


def build_term_entry(record: SummaryRecord) -> list:
    return build_term_entry_for_term(record, record.canonical_title)


def build_term_entries(record: SummaryRecord) -> list[list]:
    entries = [build_term_entry(record)]
    alias = alias_term_for_title(record.canonical_title)
    if alias is not None:
        entries.append(build_term_entry_for_term(record, alias, score=-1))
    return entries


def build_term_entry_for_term(record: SummaryRecord, term: str, score: int = 0) -> list:
    return [
        term,
        term_reading_for_term(term),
        "",
        "",
        score,
        [
            {
                "type": "structured-content",
                "content": [
                    {"tag": "div", "content": [record.summary]},
                    {
                        "tag": "div",
                        "content": [
                            "来源：萌娘百科（摘要，非全文）。",
                            {"tag": "a", "href": record.article_url, "content": ["查看原文"]},
                        ],
                    },
                ],
            }
        ],
        record.pageid,
        "",
    ]


def alias_term_for_title(title: str) -> str | None:
    match = FULLWIDTH_ALIAS_PATTERN.fullmatch(title)
    if match is None:
        return None

    base = match.group("base").rstrip()
    if not base or base == title:
        return None
    return base


def term_reading_for_term(term: str) -> str:
    pieces: list[str] = []
    converted_any = False
    last_end = 0

    for match in HANZI_RUN_PATTERN.finditer(term):
        start, end = match.span()
        if start > last_end:
            pieces.append(term[last_end:start])

        reading_parts = lazy_pinyin(
            match.group(),
            style=Style.TONE,
            strict=False,
            neutral_tone_with_five=False,
            tone_sandhi=False,
        )
        if reading_parts:
            pieces.append(" ".join(reading_parts))
            converted_any = True
        last_end = end

    if last_end < len(term):
        pieces.append(term[last_end:])

    if not converted_any:
        return ""

    return normalize_reading_text(" ".join(piece for piece in pieces if piece))


def normalize_reading_text(text: str) -> str:
    normalized = READING_COLLAPSE_SPACE_PATTERN.sub(" ", text).strip()
    normalized = READING_SPACE_BEFORE_PUNCTUATION_PATTERN.sub(r"\1", normalized)
    normalized = READING_SPACE_BEFORE_OPENING_PATTERN.sub(r"\1", normalized)
    normalized = READING_SPACE_AFTER_OPENING_PATTERN.sub(r"\1", normalized)
    normalized = READING_PUNCTUATION_WITH_TRAILING_SPACE_PATTERN.sub(r"\1 ", normalized)
    return READING_COLLAPSE_SPACE_PATTERN.sub(" ", normalized).strip()


def chunked(items: list[SummaryRecord], chunk_size: int) -> list[list[SummaryRecord]]:
    return [items[index : index + chunk_size] for index in range(0, len(items), chunk_size)]
