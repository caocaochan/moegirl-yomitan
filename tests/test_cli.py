from datetime import datetime, timezone
import json
from pathlib import Path

from moegirl_yomitan import cli
from moegirl_yomitan.config import Settings
from moegirl_yomitan.models import ManifestPage, SummaryRecord
from moegirl_yomitan.packaging import build_dictionary_content_fingerprint, save_build_state


def test_build_from_cache_skips_fetch(monkeypatch, capsys) -> None:
    output_path = Path("dist") / "cached.zip"

    monkeypatch.setattr(cli, "fetch_pages", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not fetch")))
    monkeypatch.setattr(cli, "package_dictionary", lambda settings: output_path)

    result = cli.main(["build", "--from-cache"])

    assert result == 0
    assert capsys.readouterr().out.strip() == f"Rebuilt dictionary archive from cache: {output_path}"


def test_build_without_from_cache_fetches_and_packages(monkeypatch, capsys) -> None:
    output_path = Path("dist") / "fresh.zip"
    fetch_calls: list[int | None] = []

    def fake_fetch_pages(settings, limit=None):
        fetch_calls.append(limit)
        return [object(), object(), object()]

    monkeypatch.setattr(cli, "fetch_pages", fake_fetch_pages)
    monkeypatch.setattr(cli, "package_dictionary", lambda settings: output_path)

    result = cli.main(["build", "--limit", "3"])

    assert result == 0
    assert fetch_calls == [3]
    assert capsys.readouterr().out.strip() == f"Built dictionary archive from 3 discovered pages: {output_path}"


def test_check_build_change_reports_changed_without_saved_state(tmp_path: Path, capsys) -> None:
    settings = write_single_page_cache(tmp_path)

    result = cli.main(["check-build-change", "--cache-dir", str(settings.cache_dir), "--output", str(settings.output_zip)])

    assert result == 0
    assert capsys.readouterr().out.strip().splitlines() == [
        "changed=true",
        f"fingerprint={build_dictionary_content_fingerprint(settings)}",
    ]


def test_check_build_change_reports_unchanged_when_fingerprint_matches(tmp_path: Path, capsys) -> None:
    settings = write_single_page_cache(tmp_path)
    save_build_state(settings, build_dictionary_content_fingerprint(settings))

    result = cli.main(["check-build-change", "--cache-dir", str(settings.cache_dir), "--output", str(settings.output_zip)])

    assert result == 0
    assert capsys.readouterr().out.strip().splitlines() == [
        "changed=false",
        f"fingerprint={build_dictionary_content_fingerprint(settings)}",
    ]


def test_check_build_change_reports_changed_when_fingerprint_differs(tmp_path: Path, capsys) -> None:
    settings = write_single_page_cache(tmp_path)
    save_build_state(settings, "outdated-fingerprint")

    result = cli.main(["check-build-change", "--cache-dir", str(settings.cache_dir), "--output", str(settings.output_zip)])

    assert result == 0
    assert capsys.readouterr().out.strip().splitlines() == [
        "changed=true",
        f"fingerprint={build_dictionary_content_fingerprint(settings)}",
    ]


def write_single_page_cache(tmp_path: Path) -> Settings:
    settings = Settings(
        cache_dir=tmp_path / "cache",
        output_zip=tmp_path / "dist" / "moegirl.zip",
    )
    page = ManifestPage(
        source_url="https://mzh.moegirl.org.cn/%E8%90%8C%E5%A8%98",
        title_from_url="萌娘",
        lastmod="2026-04-28T00:00:00Z",
        sitemap_url="https://mzh.moegirl.org.cn/sitemap/page.xml",
        pageid=1,
        canonical_title="萌娘",
        article_url="https://mzh.moegirl.org.cn/%E8%90%8C%E5%A8%98",
        record_path="records/1.json",
    )
    record = SummaryRecord(
        pageid=1,
        canonical_title="萌娘",
        article_url="https://mzh.moegirl.org.cn/%E8%90%8C%E5%A8%98",
        source_url="https://mzh.moegirl.org.cn/%E8%90%8C%E5%A8%98",
        lastmod="2026-04-28T00:00:00Z",
        summary="这是摘要。",
        retrieved_at=datetime.now(timezone.utc).isoformat(),
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
