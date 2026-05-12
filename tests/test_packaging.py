from datetime import date, datetime, timezone
import json
from pathlib import Path
from zipfile import ZipFile

import pytest
import requests

from moegirl_yomitan.config import Settings
from moegirl_yomitan.fetcher import AdaptiveState, adaptive_state_after_failure, adaptive_state_after_success, fetch_pages
from moegirl_yomitan.models import ManifestPage, SummaryRecord
from moegirl_yomitan.packaging import (
    alias_term_for_title,
    build_dictionary_content_fingerprint,
    build_dictionary_content_payload,
    build_index,
    build_term_entries,
    build_term_entry,
    save_build_state,
    package_dictionary,
    term_reading_for_term,
)
from moegirl_yomitan.versioning import BUILD_VERSION_ENV_VAR, load_git_build_versions, next_build_version, resolve_build_version


def test_build_term_entry_has_expected_shape() -> None:
    record = {
        "pageid": 1,
        "canonical_title": "萌娘",
        "article_url": "https://mzh.moegirl.org.cn/%E8%90%8C%E5%A8%98",
        "source_url": "https://mzh.moegirl.org.cn/%E8%90%8C%E5%A8%98",
        "lastmod": "2026-04-28T00:00:00Z",
        "summary": "这是摘要。",
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
    }
    entry = build_term_entry(type("Record", (), record)())
    assert entry[0] == "萌娘"
    assert entry[1] == "méng niáng"
    assert entry[2] == ""
    assert entry[5][0]["type"] == "structured-content"
    content = entry[5][0]["content"]
    assert content[0]["lang"] == "zh-Hans"
    assert content[1]["lang"] == "zh-Hans"
    assert entry[6] == 1


def test_build_term_entries_without_fullwidth_parentheses_returns_canonical_only() -> None:
    record = type(
        "Record",
        (),
        {
            "pageid": 1,
            "canonical_title": "萌娘",
            "article_url": "https://mzh.moegirl.org.cn/%E8%90%8C%E5%A8%98",
            "source_url": "https://mzh.moegirl.org.cn/%E8%90%8C%E5%A8%98",
            "lastmod": "2026-04-28T00:00:00Z",
            "summary": "这是摘要。",
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
        },
    )()
    entries = build_term_entries(record)
    assert len(entries) == 1
    assert entries[0][0] == "萌娘"
    assert entries[0][1] == "méng niáng"
    assert entries[0][4] == 0
    assert entries[0][6] == 1


def test_build_term_entries_adds_fullwidth_parenthetical_alias() -> None:
    record = type(
        "Record",
        (),
        {
            "pageid": 428,
            "canonical_title": "绿坝娘（和谐大色狼）",
            "article_url": "https://mzh.moegirl.org.cn/%E7%BB%BF%E5%9D%9D%E5%A8%98%EF%BC%88%E5%92%8C%E8%B0%90%E5%A4%A7%E8%89%B2%E7%8B%BC%EF%BC%89",
            "source_url": "https://mzh.moegirl.org.cn/%E7%BB%BF%E5%9D%9D%E5%A8%98%EF%BC%88%E5%92%8C%E8%B0%90%E5%A4%A7%E8%89%B2%E7%8B%BC%EF%BC%89",
            "lastmod": "2025-10-31T03:02:48Z",
            "summary": "这是摘要。",
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
        },
    )()
    entries = build_term_entries(record)
    assert [entry[0] for entry in entries] == ["绿坝娘（和谐大色狼）", "绿坝娘"]
    assert [entry[1] for entry in entries] == ["lǜ bà niáng（hé xié dà sè láng）", "lǜ bà niáng"]
    assert [entry[4] for entry in entries] == [0, -1]
    assert [entry[6] for entry in entries] == [428, 428]
    assert entries[0][5] == entries[1][5]


def test_alias_term_for_title_trims_whitespace_before_fullwidth_suffix() -> None:
    assert alias_term_for_title("绿坝娘 （和谐大色狼）") == "绿坝娘"


def test_build_term_entries_does_not_add_alias_for_ascii_parentheses() -> None:
    record = type(
        "Record",
        (),
        {
            "pageid": 100063,
            "canonical_title": "小林(希德尼娅的骑士)",
            "article_url": "https://mzh.moegirl.org.cn/%E5%B0%8F%E6%9E%97(%E5%B8%8C%E5%BE%B7%E5%B0%BC%E5%A8%85%E7%9A%84%E9%AA%91%E5%A3%AB)",
            "source_url": "https://mzh.moegirl.org.cn/%E5%B0%8F%E6%9E%97(%E5%B8%8C%E5%BE%B7%E5%B0%BC%E5%A8%85%E7%9A%84%E9%AA%91%E5%A3%AB)",
            "lastmod": "2026-02-27T12:21:45Z",
            "summary": "这是摘要。",
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
        },
    )()
    entries = build_term_entries(record)
    assert len(entries) == 1
    assert entries[0][0] == "小林(希德尼娅的骑士)"
    assert entries[0][1] == "xiǎo lín(xī dé ní yà de qí shì)"


def test_term_reading_for_term_keeps_mixed_script_punctuation_inline() -> None:
    assert term_reading_for_term("战舰少女:百眼巨人") == "zhàn jiàn shào nǚ: bǎi yǎn jù rén"


def test_term_reading_for_term_omits_non_hanzi_words_from_mixed_titles() -> None:
    assert term_reading_for_term("舰队Collection") == "jiàn duì"


def test_term_reading_for_term_omits_non_hanzi_word_prefixes() -> None:
    assert term_reading_for_term("Collection舰队") == "jiàn duì"


def test_term_reading_for_term_uses_phrase_data_for_sister_title() -> None:
    assert term_reading_for_term("姐姐") == "jiě jie"


def test_term_reading_for_term_preserves_phrase_reading_with_longer_terms() -> None:
    assert term_reading_for_term("姐姐大人") == "jiě jie dà rén"


def test_term_reading_for_term_returns_empty_when_no_hanzi_are_present() -> None:
    assert term_reading_for_term("Argus") == ""


def test_build_index_uses_current_date_for_version_fields() -> None:
    settings = Settings()
    index = build_index(settings, revision="2026.04.29")
    assert index["revision"] == "2026.04.29"
    assert index["isUpdatable"] is True
    assert index["indexUrl"] == settings.dictionary_update_index_url
    assert index["downloadUrl"] == settings.dictionary_update_download_url
    assert "version" not in index


def test_dictionary_content_payload_omits_revision_field(tmp_path: Path) -> None:
    settings = Settings(
        cache_dir=tmp_path / "cache",
        output_zip=tmp_path / "dist" / "moegirl.zip",
    )
    payload = build_dictionary_content_payload(settings)
    assert "revision" not in payload["index"]


def test_next_build_version_uses_plain_date_for_first_build() -> None:
    assert next_build_version(existing_versions=[], today=date(2026, 4, 29)) == "2026.04.29"


def test_next_build_version_appends_suffix_for_repeat_builds() -> None:
    assert next_build_version(existing_versions=["2026.04.29"], today=date(2026, 4, 29)) == "2026.04.29.1"
    assert next_build_version(
        existing_versions=["2026.04.29", "2026.04.29.1", "2026.04.29.3"],
        today=date(2026, 4, 29),
    ) == "2026.04.29.4"


def test_resolve_build_version_prefers_explicit_environment_override() -> None:
    version = resolve_build_version(
        env={BUILD_VERSION_ENV_VAR: "2026.04.29.7"},
        today=date(2026, 4, 29),
        existing_versions=["2026.04.29"],
    )
    assert version == "2026.04.29.7"


def test_load_git_build_versions_accepts_plain_and_prefixed_tags(monkeypatch) -> None:
    class Result:
        returncode = 0
        stdout = "\n".join(["2026.04.29", "v2026.04.29.1", "not-a-build-tag"])

    monkeypatch.setattr("moegirl_yomitan.versioning.subprocess.run", lambda *args, **kwargs: Result())

    assert load_git_build_versions() == ["2026.04.29", "2026.04.29.1"]


def test_adaptive_state_reduces_concurrency_and_adds_cooldown_on_failure() -> None:
    settings = Settings(concurrency=4, min_concurrency=1, backoff_base_seconds=1.0, adaptive_backoff_cap_seconds=30.0)
    state = AdaptiveState(current_concurrency=4, consecutive_successes=1, cooldown_seconds=0.0)
    next_state = adaptive_state_after_failure(settings, state)
    assert next_state.current_concurrency == 3
    assert next_state.consecutive_successes == 0
    assert next_state.cooldown_seconds == 1.0


def test_adaptive_state_recovers_concurrency_after_successes() -> None:
    settings = Settings(concurrency=4, min_concurrency=1, backoff_base_seconds=1.0, adaptive_backoff_cap_seconds=30.0)
    state = AdaptiveState(current_concurrency=2, consecutive_successes=0, cooldown_seconds=2.0)
    state = adaptive_state_after_success(settings, state)
    assert state.current_concurrency == 2
    assert state.consecutive_successes == 1
    assert state.cooldown_seconds == 1.0
    state = adaptive_state_after_success(settings, state)
    assert state.current_concurrency == 3
    assert state.consecutive_successes == 0
    assert state.cooldown_seconds == 0.0


def test_smoke_build_and_package(tmp_path: Path) -> None:
    pytest.importorskip("jsonschema")
    settings = Settings(
        cache_dir=tmp_path / "cache",
        output_zip=tmp_path / "dist" / "moegirl.zip",
        batch_size=3,
        concurrency=1,
    )
    try:
        pages = fetch_pages(settings, limit=3)
    except requests.RequestException as exc:
        pytest.skip(f"Skipping live smoke build because the remote wiki was flaky: {exc}")
    assert len(pages) == 3

    output_path = package_dictionary(settings)
    assert output_path.exists()
    assert settings.output_index.exists()

    with ZipFile(output_path) as archive:
        names = sorted(archive.namelist())
        assert names == ["index.json", "term_bank_1.json"]

        index_data = json.loads(archive.read("index.json").decode("utf-8"))
        term_data = json.loads(archive.read("term_bank_1.json").decode("utf-8"))
    standalone_index_data = json.loads(settings.output_index.read_text(encoding="utf-8"))

    validate_against_official_schema(index_data, term_data)
    validate_index_against_official_schema(standalone_index_data)
    assert standalone_index_data == index_data
    assert term_data
    assert all(entry[5][0]["type"] == "structured-content" for entry in term_data)


def test_package_dictionary_writes_zip_and_standalone_index_with_shared_revision(tmp_path: Path, monkeypatch) -> None:
    pytest.importorskip("jsonschema")
    settings = write_single_page_cache(tmp_path)
    monkeypatch.setattr("moegirl_yomitan.packaging.resolve_build_version", lambda: "2026.04.29.2")

    output_path = package_dictionary(settings)

    assert output_path.exists()
    assert settings.output_index == tmp_path / "dist" / "moegirl-yomitan-index.json"
    assert settings.output_index.exists()

    with ZipFile(output_path) as archive:
        names = sorted(archive.namelist())
        assert names == ["index.json", "term_bank_1.json"]
        index_data = json.loads(archive.read("index.json").decode("utf-8"))
        term_data = json.loads(archive.read("term_bank_1.json").decode("utf-8"))

    standalone_index_data = json.loads(settings.output_index.read_text(encoding="utf-8"))

    validate_against_official_schema(index_data, term_data)
    validate_index_against_official_schema(standalone_index_data)
    assert index_data == standalone_index_data
    assert index_data["revision"] == "2026.04.29.2"


def test_dictionary_content_fingerprint_ignores_retrieved_at(tmp_path: Path) -> None:
    first = write_single_page_cache(
        tmp_path / "first",
        retrieved_at="2026-04-28T12:00:00+00:00",
    )
    second = write_single_page_cache(
        tmp_path / "second",
        retrieved_at="2026-04-29T12:00:00+00:00",
    )

    assert build_dictionary_content_fingerprint(first) == build_dictionary_content_fingerprint(second)


def test_dictionary_content_fingerprint_changes_when_summary_changes(tmp_path: Path) -> None:
    first = write_single_page_cache(tmp_path / "first", summary="这是摘要。")
    second = write_single_page_cache(tmp_path / "second", summary="这是新摘要。")

    assert build_dictionary_content_fingerprint(first) != build_dictionary_content_fingerprint(second)


def test_dictionary_content_fingerprint_changes_when_alias_output_changes(tmp_path: Path) -> None:
    first = write_single_page_cache(tmp_path / "first", canonical_title="绿坝娘（和谐大色狼）")
    second = write_single_page_cache(tmp_path / "second", canonical_title="绿坝娘")

    assert build_dictionary_content_fingerprint(first) != build_dictionary_content_fingerprint(second)


def test_dictionary_content_fingerprint_reuses_cached_record_fingerprint(tmp_path: Path, monkeypatch) -> None:
    settings = write_single_page_cache(tmp_path)
    fingerprint = build_dictionary_content_fingerprint(settings)
    save_build_state(settings, fingerprint)

    def fail_build_term_entries(record):
        raise AssertionError("unchanged record should reuse cached fingerprint")

    monkeypatch.setattr("moegirl_yomitan.packaging.build_term_entries", fail_build_term_entries)

    assert build_dictionary_content_fingerprint(settings) == fingerprint


def test_dictionary_content_fingerprint_recomputes_only_changed_record(tmp_path: Path, monkeypatch) -> None:
    settings = write_single_page_cache(tmp_path)
    append_cache_record(settings, pageid=2, canonical_title="测试娘", summary="旧摘要。")
    save_build_state(settings, build_dictionary_content_fingerprint(settings))
    append_cache_record(settings, pageid=2, canonical_title="测试娘", summary="这是新的摘要。")

    calls: list[int] = []
    original_build_term_entries = build_term_entries

    def tracking_build_term_entries(record):
        calls.append(record.pageid)
        return original_build_term_entries(record)

    monkeypatch.setattr("moegirl_yomitan.packaging.build_term_entries", tracking_build_term_entries)

    build_dictionary_content_fingerprint(settings)

    assert calls == [2]


def test_dictionary_content_fingerprint_recomputes_when_lastmod_changes(tmp_path: Path, monkeypatch) -> None:
    settings = write_single_page_cache(tmp_path)
    save_build_state(settings, build_dictionary_content_fingerprint(settings))
    rewrite_single_page_cache(settings, lastmod="2026-04-29T00:00:00Z")
    calls: list[int] = []
    original_build_term_entries = build_term_entries

    def tracking_build_term_entries(record):
        calls.append(record.pageid)
        return original_build_term_entries(record)

    monkeypatch.setattr("moegirl_yomitan.packaging.build_term_entries", tracking_build_term_entries)

    build_dictionary_content_fingerprint(settings)

    assert calls == [1]


def test_dictionary_content_fingerprint_recomputes_when_algorithm_changes(tmp_path: Path, monkeypatch) -> None:
    settings = write_single_page_cache(tmp_path)
    save_build_state(settings, build_dictionary_content_fingerprint(settings))
    calls: list[int] = []
    original_build_term_entries = build_term_entries

    def tracking_build_term_entries(record):
        calls.append(record.pageid)
        return original_build_term_entries(record)

    monkeypatch.setattr("moegirl_yomitan.packaging.FINGERPRINT_ALGORITHM_VERSION", "packaged-content-test")
    monkeypatch.setattr("moegirl_yomitan.packaging.build_term_entries", tracking_build_term_entries)

    build_dictionary_content_fingerprint(settings)

    assert calls == [1]


def test_dictionary_content_fingerprint_ignores_malformed_build_state(tmp_path: Path) -> None:
    settings = write_single_page_cache(tmp_path)
    settings.build_state_path.write_text("{not json", encoding="utf-8")

    assert build_dictionary_content_fingerprint(settings)


def validate_against_official_schema(index_data: dict, term_data: list) -> None:
    import jsonschema

    index_schema = load_official_index_schema()
    term_schema = requests.get(
        "https://raw.githubusercontent.com/yomidevs/yomitan/refs/heads/master/ext/data/schemas/dictionary-term-bank-v3-schema.json",
        timeout=30,
    ).json()

    jsonschema.validate(index_data, index_schema)
    jsonschema.validate(term_data, term_schema)


def validate_index_against_official_schema(index_data: dict) -> None:
    import jsonschema

    jsonschema.validate(index_data, load_official_index_schema())


def load_official_index_schema() -> dict:
    return requests.get(
        "https://raw.githubusercontent.com/yomidevs/yomitan/refs/heads/master/ext/data/schemas/dictionary-index-schema.json",
        timeout=30,
    ).json()


def write_single_page_cache(
    tmp_path: Path,
    *,
    canonical_title: str = "萌娘",
    summary: str = "这是摘要。",
    retrieved_at: str | None = None,
) -> Settings:
    settings = Settings(
        cache_dir=tmp_path / "cache",
        output_zip=tmp_path / "dist" / "moegirl.zip",
        batch_size=3,
        concurrency=1,
    )
    source_url = "https://mzh.moegirl.org.cn/%E8%90%8C%E5%A8%98"
    page = ManifestPage(
        source_url=source_url,
        title_from_url=canonical_title,
        lastmod="2026-04-28T00:00:00Z",
        sitemap_url="https://mzh.moegirl.org.cn/sitemap/page.xml",
        pageid=1,
        canonical_title=canonical_title,
        article_url=source_url,
        record_path="records/1.json",
    )
    record = SummaryRecord(
        pageid=1,
        canonical_title=canonical_title,
        article_url=source_url,
        source_url=source_url,
        lastmod="2026-04-28T00:00:00Z",
        summary=summary,
        retrieved_at=retrieved_at or datetime.now(timezone.utc).isoformat(),
    )
    settings.cache_dir.mkdir(parents=True, exist_ok=True)
    settings.records_dir.mkdir(parents=True, exist_ok=True)
    settings.manifest_path.write_text(
        json.dumps({"pages": [page.to_dict()]}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    settings.records_dir.joinpath("1.json").write_text(
        json.dumps(record.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return settings


def append_cache_record(
    settings: Settings,
    *,
    pageid: int,
    canonical_title: str,
    summary: str,
    lastmod: str = "2026-04-28T00:00:00Z",
) -> None:
    manifest = json.loads(settings.manifest_path.read_text(encoding="utf-8"))
    source_url = f"https://mzh.moegirl.org.cn/{pageid}"
    page = ManifestPage(
        source_url=source_url,
        title_from_url=canonical_title,
        lastmod=lastmod,
        sitemap_url="https://mzh.moegirl.org.cn/sitemap/page.xml",
        pageid=pageid,
        canonical_title=canonical_title,
        article_url=source_url,
        record_path=f"records/{pageid}.json",
    )
    record = SummaryRecord(
        pageid=pageid,
        canonical_title=canonical_title,
        article_url=source_url,
        source_url=source_url,
        lastmod=lastmod,
        summary=summary,
        retrieved_at=datetime.now(timezone.utc).isoformat(),
    )

    manifest["pages"] = [item for item in manifest["pages"] if item.get("pageid") != pageid]
    manifest["pages"].append(page.to_dict())
    settings.manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    settings.records_dir.joinpath(f"{pageid}.json").write_text(
        json.dumps(record.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def rewrite_single_page_cache(settings: Settings, *, lastmod: str) -> None:
    manifest = json.loads(settings.manifest_path.read_text(encoding="utf-8"))
    manifest["pages"][0]["lastmod"] = lastmod
    settings.manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    record_path = settings.records_dir.joinpath("1.json")
    record = json.loads(record_path.read_text(encoding="utf-8"))
    record["lastmod"] = lastmod
    record_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
