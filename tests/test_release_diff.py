import json
from io import BytesIO
from zipfile import ZipFile

import pytest

import moegirl_yomitan.release_diff as release_diff
from moegirl_yomitan.release_diff import (
    ReleaseAsset,
    ReleaseEntry,
    ReleaseEntryDiff,
    build_release_diff_html,
    diff_added_entries,
    load_release_entries,
    render_release_diff_html,
    select_base_dictionary_release,
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


def test_select_base_dictionary_release_uses_highest_lower_semantic_version() -> None:
    base = select_base_dictionary_release(
        [
            make_release("2026.06.20"),
            make_release("not-a-version"),
            make_release("2026.06.19", draft=True),
            make_release("2026.06.18", asset_name="other.zip"),
            make_release("2026.05.12"),
            make_release("2026.06.05.1"),
            make_release("2026.06.05.2"),
            make_release("2026.06.05"),
        ],
        head_version="2026.06.20",
    )

    assert base.tag_name == "2026.06.05.2"


def test_select_base_dictionary_release_requires_an_earlier_dictionary_release() -> None:
    with pytest.raises(ValueError, match="No published dictionary release exists before 2026.06.20"):
        select_base_dictionary_release(
            [
                make_release("2026.06.20"),
                make_release("2026.06.19", draft=True),
                make_release("2026.06.18", asset_name="other.zip"),
                make_release("invalid"),
            ],
            head_version="2026.06.20",
        )


def test_build_release_diff_html_uses_local_head_zip(tmp_path, monkeypatch) -> None:
    base_zip = make_zip({"term_bank_1.json": [make_term_entry("旧条目", 1)]})
    head_zip = tmp_path / "head.zip"
    head_zip.write_bytes(
        make_zip({"term_bank_1.json": [make_term_entry("旧条目", 1), make_term_entry("新条目", 2)]})
    )
    monkeypatch.setattr(release_diff, "fetch_github_releases", lambda session: [make_release("2026.06.05.2")])
    monkeypatch.setattr(release_diff, "download_release_asset", lambda session, release: base_zip)

    html = build_release_diff_html(head_version="2026.06.20", head_zip=head_zip)

    assert "<title>Added entries in 2026.06.20</title>" in html
    assert ">2026.06.05.2</a>" in html
    assert ">2026.06.20</a>" in html
    assert "<p>Added entries: 1</p>" in html
    assert "新条目" in html


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


def test_render_release_diff_html_includes_versions_count_and_entries() -> None:
    html = render_release_diff_html(
        ReleaseEntryDiff(
            base=ReleaseAsset("2026.05.12", "https://example.invalid/base", "https://example.invalid/base.zip"),
            head=ReleaseAsset("2026.06.05", "https://example.invalid/head", "https://example.invalid/head.zip"),
            added=[
                ReleaseEntry(pageid=4, title="甲", article_url="https://example.invalid/4"),
                ReleaseEntry(pageid=3, title="乙"),
            ],
        )
    )

    assert "<!doctype html>" in html
    assert "<title>Added entries in 2026.06.05</title>" in html
    assert '<a href="https://example.invalid/base">2026.05.12</a>' in html
    assert '<a href="https://example.invalid/head">2026.06.05</a>' in html
    assert "<p>Added entries: 2</p>" in html
    assert '<li><a href="https://example.invalid/4">甲</a> <code>pageid=4</code></li>' in html
    assert "<li>乙 <code>pageid=3</code></li>" in html


def test_render_release_diff_html_escapes_entry_text() -> None:
    html = render_release_diff_html(
        ReleaseEntryDiff(
            base=ReleaseAsset("2026.05.12", "https://example.invalid/base", "https://example.invalid/base.zip"),
            head=ReleaseAsset("2026.06.05", "https://example.invalid/head", "https://example.invalid/head.zip"),
            added=[ReleaseEntry(pageid=5, title="<script>", article_url='https://example.invalid/"quoted"')],
        )
    )

    assert "&lt;script&gt;" in html
    assert 'href="https://example.invalid/&quot;quoted&quot;"' in html
