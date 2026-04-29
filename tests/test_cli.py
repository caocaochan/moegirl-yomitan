from pathlib import Path

from moegirl_yomitan import cli


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
