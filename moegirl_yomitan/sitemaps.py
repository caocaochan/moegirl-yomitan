from __future__ import annotations

from typing import Iterable
from urllib.parse import unquote, urlsplit, urlunsplit
import re
import xml.etree.ElementTree as ET

from .models import ManifestPage


_SITEMAP_NAMESPACE = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
_URL_ENTRY_RE = re.compile(
    r"<url>\s*<loc>(.*?)</loc>\s*<lastmod>(.*?)</lastmod>\s*(?:<priority>(.*?)</priority>\s*)?</url>",
    re.S,
)


def xml_has_closing_root(xml_text: str, root_name: str) -> bool:
    stripped = xml_text.rstrip()
    return stripped.endswith(f"</{root_name}>")


def parse_namespace_zero_sitemaps(xml_text: str) -> list[str]:
    root = ET.fromstring(xml_text)
    urls: list[str] = []
    for node in root.findall("sm:sitemap", _SITEMAP_NAMESPACE):
        loc = node.findtext("sm:loc", default="", namespaces=_SITEMAP_NAMESPACE)
        if "NS_0-" in loc:
            urls.append(loc)
    return sorted(urls)


def parse_sitemap_entries(xml_text: str, sitemap_url: str) -> list[ManifestPage]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return parse_partial_sitemap_entries(xml_text, sitemap_url)

    pages: list[ManifestPage] = []
    for node in root.findall("sm:url", _SITEMAP_NAMESPACE):
        loc = node.findtext("sm:loc", default="", namespaces=_SITEMAP_NAMESPACE)
        lastmod = node.findtext("sm:lastmod", default="", namespaces=_SITEMAP_NAMESPACE)
        if not loc:
            continue
        pages.append(
            ManifestPage(
                source_url=rewrite_to_mzh(loc),
                title_from_url=decode_title_from_url(loc),
                lastmod=lastmod,
                sitemap_url=sitemap_url,
            )
        )
    return pages


def parse_partial_sitemap_entries(xml_text: str, sitemap_url: str) -> list[ManifestPage]:
    pages: list[ManifestPage] = []
    for match in _URL_ENTRY_RE.finditer(xml_text):
        loc = match.group(1)
        lastmod = match.group(2)
        if not loc:
            continue
        pages.append(
            ManifestPage(
                source_url=rewrite_to_mzh(loc),
                title_from_url=decode_title_from_url(loc),
                lastmod=lastmod,
                sitemap_url=sitemap_url,
            )
        )
    return pages


def decode_title_from_url(url: str) -> str:
    path = urlsplit(url).path.lstrip("/")
    return unquote(path)


def rewrite_to_mzh(url: str) -> str:
    split = urlsplit(url)
    return urlunsplit((split.scheme or "https", "mzh.moegirl.org.cn", split.path, split.query, split.fragment))


def canonical_article_url(title: str) -> str:
    # Preserve MediaWiki path semantics while ensuring non-ASCII characters are encoded.
    split = urlsplit(f"https://mzh.moegirl.org.cn/{title}")
    return urlunsplit((split.scheme, split.netloc, split.path, split.query, split.fragment))


def merge_manifest_pages(current_pages: Iterable[ManifestPage], previous_pages: dict[str, ManifestPage]) -> list[ManifestPage]:
    merged: list[ManifestPage] = []
    for page in current_pages:
        previous = previous_pages.get(page.source_url)
        if previous is not None:
            page.pageid = previous.pageid
            page.canonical_title = previous.canonical_title
            page.article_url = previous.article_url
            page.record_path = previous.record_path
        merged.append(page)
    merged.sort(key=lambda item: item.source_url)
    return merged
