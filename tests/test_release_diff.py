import json
from io import BytesIO
from zipfile import ZipFile

import pytest

from moegirl_yomitan.release_diff import (
    ReleaseAsset,
    ReleaseEntry,
    ReleaseEntryDiff,
    diff_added_entries,
    load_release_entries,
    render_release_diff_markdown,
    select_latest_two_dictionary_releases,
)


def make_release(tag: str, *, draft: bool = False, asset_name: str = "moegirl-yomitan.zip") -> dict:
    return {
        "tag_name": tag,
        "html_url": f"https://github.com/caocaochan/moegirl-yomitan/releases/tag/{tag}",
        "draft": draft,
        "assets": [
            {
                "name": asset_name,
                "browser_download_url": f"https://github.com/caocaochan/moegirl-yomitan/releases/download/{tag}/{asset_name}",
            }
        ],
    }


def make_term_entry(title: str, pageid: int, *, score: int = 0, href: str | None = None) -> list:
    return [
        title,
        "",
        "",
        "",
        score,
        [
            {
                "type": "structured-content",
                "content": [
                    {"tag": "div", "content": ["摘要"]},
                    {"tag": "a", "href": href or f"https://mzh.moegirl.org.cn/{pageid}", "content": ["查看原文"]},
                ],
            }
        ],
        pageid,
        "",
    ]


def make_zip(term_banks: dict[str, list]) -> bytes:
    buffer = BytesIO()
    with ZipFile(buffer, "w") as archive:
        archive.writestr("index.json", "{}")
        for name, entries in term_banks.items():
            archive.writestr(name, json.dumps(entries, ensure_ascii=False))
    return buffer.getvalue()


def test_select_latest_two_dictionary_releases_ignores_drafts() -> None:
    base, head = select_latest_two_dictionary_releases(
        [
            make_release("draft", draft=True),
            make_release("2026.06.05"),
            make_release("2026.05.12"),
        ]
    )

    assert base.tag_name == "2026.05.12"
    assert head.tag_name == "2026.06.05"


def test_select_latest_two_dictionary_releases_requires_dictionary_asset() -> None:
    with pytest.raises(ValueError, match="does not include moegirl-yomitan.zip"):
        select_latest_two_dictionary_releases(
            [
                make_release("2026.06.05", asset_name="other.zip"),
                make_release("2026.05.12"),
            ]
        )


def test_load_release_entries_reads_term_banks_and_ignores_duplicate_alias_rows() -> None:
    entries = load_release_entries(
        make_zip(
            {
                "term_bank_2.json": [make_term_entry("乙", 2)],
                "term_bank_1.json": [
                    make_term_entry("萌娘", 1, score=0, href="https://example.invalid/main"),
                    make_term_entry("萌", 1, score=-1, href="https://example.invalid/alias"),
                ],
            }
        )
    )

    assert sorted(entries) == [1, 2]
    assert entries[1] == ReleaseEntry(
        pageid=1,
        title="萌娘",
        article_url="https://example.invalid/main",
        score=0,
    )


def test_diff_added_entries_uses_pageids_and_sorts_by_title() -> None:
    base_entries = {
        1: ReleaseEntry(pageid=1, title="萌娘"),
        2: ReleaseEntry(pageid=2, title="旧条目"),
    }
    head_entries = {
        1: ReleaseEntry(pageid=1, title="萌娘"),
        3: ReleaseEntry(pageid=3, title="乙"),
        4: ReleaseEntry(pageid=4, title="甲"),
    }

    added = diff_added_entries(base_entries, head_entries)

    assert [(entry.pageid, entry.title) for entry in added] == [(3, "乙"), (4, "甲")]


def test_render_release_diff_markdown_includes_versions_count_and_entries() -> None:
    markdown = render_release_diff_markdown(
        ReleaseEntryDiff(
            base=ReleaseAsset("2026.05.12", "https://example.invalid/base", "https://example.invalid/base.zip"),
            head=ReleaseAsset("2026.06.05", "https://example.invalid/head", "https://example.invalid/head.zip"),
            added=[
                ReleaseEntry(pageid=4, title="甲", article_url="https://example.invalid/4"),
                ReleaseEntry(pageid=3, title="乙"),
            ],
        )
    )

    assert markdown.splitlines() == [
        "## Added entries in 2026.06.05",
        "",
        "Compared [2026.05.12](https://example.invalid/base) -> [2026.06.05](https://example.invalid/head).",
        "",
        "Added entries: 2",
        "",
        "- [甲](https://example.invalid/4) (`pageid=4`)",
        "- 乙 (`pageid=3`)",
    ]
