from __future__ import annotations

from dataclasses import dataclass
from html import escape
from io import BytesIO
import json
from pathlib import Path
from typing import Any
from zipfile import ZipFile

import requests

from .versioning import BUILD_VERSION_PATTERN


GITHUB_REPOSITORY = "caocaochan/moegirl-yomitan"
DICTIONARY_ASSET_NAME = "moegirl-yomitan.zip"
USER_AGENT = "moegirl-yomitan-builder/0.1 (+release diff)"


@dataclass(frozen=True)
class ReleaseAsset:
    tag_name: str
    html_url: str
    download_url: str


@dataclass(frozen=True)
class ReleaseEntry:
    pageid: int
    title: str
    article_url: str | None = None
    score: int = 0


@dataclass(frozen=True)
class ReleaseEntryDiff:
    base: ReleaseAsset
    head: ReleaseAsset
    added: list[ReleaseEntry]


def build_release_diff_html(*, head_version: str, head_zip: Path) -> str:
    if build_version_sort_key(head_version) is None:
        raise ValueError(f"Invalid build version: {head_version}")

    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    try:
        releases = fetch_github_releases(session)
        base = select_base_dictionary_release(releases, head_version=head_version)
        base_entries = load_release_entries(download_release_asset(session, base))
        head_entries = load_release_entries(head_zip.read_bytes())
    finally:
        session.close()

    head = ReleaseAsset(
        tag_name=head_version,
        html_url=f"https://github.com/{GITHUB_REPOSITORY}/releases/tag/{head_version}",
        download_url=(
            f"https://github.com/{GITHUB_REPOSITORY}/releases/download/{head_version}/{DICTIONARY_ASSET_NAME}"
        ),
    )
    return render_release_diff_html(
        ReleaseEntryDiff(
            base=base,
            head=head,
            added=diff_added_entries(base_entries, head_entries),
        )
    )


def fetch_github_releases(session: requests.Session) -> list[dict[str, Any]]:
    response = session.get(f"https://api.github.com/repos/{GITHUB_REPOSITORY}/releases", params={"per_page": "100"})
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, list):
        raise ValueError("GitHub releases response was not a list.")
    return data


def select_base_dictionary_release(releases: list[dict[str, Any]], *, head_version: str) -> ReleaseAsset:
    head_key = build_version_sort_key(head_version)
    if head_key is None:
        raise ValueError(f"Invalid build version: {head_version}")

    candidates: list[tuple[tuple[int, int, int, int], ReleaseAsset]] = []
    for release in releases:
        if release.get("draft"):
            continue
        tag_name = release.get("tag_name")
        if not isinstance(tag_name, str):
            continue
        version_key = build_version_sort_key(tag_name)
        if version_key is None or version_key >= head_key:
            continue
        try:
            asset = release_asset_from_release(release)
        except ValueError:
            continue
        candidates.append((version_key, asset))

    if not candidates:
        raise ValueError(f"No published dictionary release exists before {head_version}.")
    return max(candidates, key=lambda candidate: candidate[0])[1]


def build_version_sort_key(version: str) -> tuple[int, int, int, int] | None:
    match = BUILD_VERSION_PATTERN.fullmatch(version.strip())
    if match is None:
        return None
    year, month, day = (int(part) for part in match.group("build_date").split("."))
    sequence = int(match.group("sequence") or "0")
    return year, month, day, sequence


def release_asset_from_release(release: dict[str, Any]) -> ReleaseAsset:
    tag_name = release.get("tag_name")
    html_url = release.get("html_url")
    assets = release.get("assets")
    if not isinstance(tag_name, str) or not tag_name:
        raise ValueError("GitHub release is missing tag_name.")
    if not isinstance(html_url, str) or not html_url:
        raise ValueError(f"GitHub release {tag_name} is missing html_url.")
    if not isinstance(assets, list):
        raise ValueError(f"GitHub release {tag_name} is missing assets.")

    for asset in assets:
        if not isinstance(asset, dict) or asset.get("name") != DICTIONARY_ASSET_NAME:
            continue
        download_url = asset.get("browser_download_url")
        if isinstance(download_url, str) and download_url:
            return ReleaseAsset(tag_name=tag_name, html_url=html_url, download_url=download_url)

    raise ValueError(f"GitHub release {tag_name} does not include {DICTIONARY_ASSET_NAME}.")


def download_release_asset(session: requests.Session, release: ReleaseAsset) -> bytes:
    response = session.get(release.download_url)
    response.raise_for_status()
    return response.content


def load_release_entries(zip_bytes: bytes) -> dict[int, ReleaseEntry]:
    entries: dict[int, ReleaseEntry] = {}
    with ZipFile(BytesIO(zip_bytes)) as archive:
        for name in sorted(archive.namelist()):
            if not is_term_bank_name(name):
                continue
            term_entries = json.loads(archive.read(name).decode("utf-8"))
            if not isinstance(term_entries, list):
                continue
            for raw_entry in term_entries:
                entry = release_entry_from_term_entry(raw_entry)
                if entry is None:
                    continue
                existing = entries.get(entry.pageid)
                if existing is None or entry_sort_key(entry) < entry_sort_key(existing):
                    entries[entry.pageid] = entry
    return entries


def is_term_bank_name(name: str) -> bool:
    return name.startswith("term_bank_") and name.endswith(".json")


def release_entry_from_term_entry(raw_entry: Any) -> ReleaseEntry | None:
    if not isinstance(raw_entry, list) or len(raw_entry) <= 6:
        return None
    title = raw_entry[0]
    score = raw_entry[4]
    pageid = raw_entry[6]
    if not isinstance(title, str) or not isinstance(pageid, int):
        return None
    if not isinstance(score, int):
        score = 0
    return ReleaseEntry(
        pageid=pageid,
        title=title,
        article_url=extract_first_href(raw_entry[5]) if len(raw_entry) > 5 else None,
        score=score,
    )


def entry_sort_key(entry: ReleaseEntry) -> tuple[int, str]:
    return (-entry.score, entry.title.casefold())


def extract_first_href(value: Any) -> str | None:
    if isinstance(value, dict):
        href = value.get("href")
        if isinstance(href, str) and href:
            return href
        for child in value.values():
            found = extract_first_href(child)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = extract_first_href(child)
            if found is not None:
                return found
    return None


def diff_added_entries(base_entries: dict[int, ReleaseEntry], head_entries: dict[int, ReleaseEntry]) -> list[ReleaseEntry]:
    added_pageids = set(head_entries) - set(base_entries)
    return sorted((head_entries[pageid] for pageid in added_pageids), key=lambda entry: (entry.title.casefold(), entry.pageid))


def render_release_diff_html(diff: ReleaseEntryDiff) -> str:
    title = f"Added entries in {diff.head.tag_name}"
    lines = [
        "<!doctype html>",
        '<html lang="en">',
        "<head>",
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>{escape(title)}</title>",
        "<style>",
        "body{font-family:system-ui,-apple-system,Segoe UI,sans-serif;line-height:1.5;margin:2rem;max-width:72rem}",
        "h1{font-size:1.8rem}a{color:#0969da}code{background:#f6f8fa;padding:.1rem .3rem;border-radius:.25rem}",
        "li{margin:.25rem 0}",
        "</style>",
        "</head>",
        "<body>",
        f"<h1>{escape(title)}</h1>",
        (
            f'<p>Compared <a href="{escape(diff.base.html_url, quote=True)}">{escape(diff.base.tag_name)}</a> '
            f'to <a href="{escape(diff.head.html_url, quote=True)}">{escape(diff.head.tag_name)}</a>.</p>'
        ),
        f"<p>Added entries: {len(diff.added)}</p>",
    ]

    if diff.added:
        lines.append("<ul>")
        for entry in diff.added:
            title_text = escape(entry.title)
            if entry.article_url:
                rendered_title = f'<a href="{escape(entry.article_url, quote=True)}">{title_text}</a>'
            else:
                rendered_title = title_text
            lines.append(f"<li>{rendered_title} <code>pageid={entry.pageid}</code></li>")
        lines.append("</ul>")
    else:
        lines.append("<p>No added entries.</p>")

    lines.extend(["</body>", "</html>"])
    return "\n".join(lines)
