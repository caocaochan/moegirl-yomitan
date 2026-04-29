from moegirl_yomitan.sitemaps import (
    decode_title_from_url,
    parse_namespace_zero_sitemaps,
    parse_partial_sitemap_entries,
    parse_sitemap_entries,
    rewrite_to_mzh,
    xml_has_closing_root,
)
from moegirl_yomitan.fetcher import sitemap_url_candidates


def test_parse_namespace_zero_sitemaps_filters_other_namespaces() -> None:
    xml = """<?xml version="1.0" encoding="UTF-8"?>
    <sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <sitemap><loc>https://zh.moegirl.org.cn/sitemap/sitemap-zhmoegirl-NS_0-0.xml</loc></sitemap>
      <sitemap><loc>https://zh.moegirl.org.cn/sitemap/sitemap-zhmoegirl-NS_4-0.xml</loc></sitemap>
      <sitemap><loc>https://zh.moegirl.org.cn/sitemap/sitemap-zhmoegirl-NS_0-1.xml</loc></sitemap>
    </sitemapindex>
    """
    assert parse_namespace_zero_sitemaps(xml) == [
        "https://zh.moegirl.org.cn/sitemap/sitemap-zhmoegirl-NS_0-0.xml",
        "https://zh.moegirl.org.cn/sitemap/sitemap-zhmoegirl-NS_0-1.xml",
    ]


def test_parse_sitemap_entries_decodes_and_rewrites() -> None:
    xml = """<?xml version="1.0" encoding="UTF-8"?>
    <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <url>
        <loc>https://zh.moegirl.org.cn/%21%21%21Dream_Cooking%21%21%21</loc>
        <lastmod>2026-04-25T06:40:03Z</lastmod>
      </url>
    </urlset>
    """
    entry = parse_sitemap_entries(xml, "https://example.invalid/sitemap.xml")[0]
    assert entry.title_from_url == "!!!Dream_Cooking!!!"
    assert entry.source_url == "https://mzh.moegirl.org.cn/%21%21%21Dream_Cooking%21%21%21"
    assert entry.lastmod == "2026-04-25T06:40:03Z"


def test_decode_title_from_url_handles_non_ascii() -> None:
    assert decode_title_from_url("https://zh.moegirl.org.cn/%E8%90%8C%E5%A8%98") == "萌娘"


def test_rewrite_to_mzh_preserves_path() -> None:
    assert rewrite_to_mzh("https://zh.moegirl.org.cn/%E8%90%8C%E5%A8%98") == "https://mzh.moegirl.org.cn/%E8%90%8C%E5%A8%98"


def test_xml_has_closing_root_detects_truncation() -> None:
    assert xml_has_closing_root("<urlset><url></url></urlset>\n", "urlset") is True
    assert xml_has_closing_root("<urlset><url></url>", "urlset") is False


def test_parse_partial_sitemap_entries_recovers_complete_items() -> None:
    truncated_xml = """<?xml version="1.0" encoding="UTF-8"?>
    <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <url>
        <loc>https://zh.moegirl.org.cn/%E8%90%8C%E5%A8%98</loc>
        <lastmod>2026-04-25T06:40:03Z</lastmod>
        <priority>1.0</priority>
      </url>
      <url>
        <loc>https://zh.moegirl.org.cn/%E5%8F%B2%E9%83%BD%E5%8D%8E%E5%BE%B7</loc>
        <lastmod>2026-04-24T06:40:03Z</lastmod>
        <priority>1.0</priority>
      </url>
      <url><loc>https://zh.moegirl.org.cn/%E6%AE%8B"""
    entries = parse_partial_sitemap_entries(truncated_xml, "https://example.invalid/sitemap.xml")
    assert [entry.title_from_url for entry in entries] == ["萌娘", "史都华德"]


def test_sitemap_url_candidates_try_other_host_before_output_variant() -> None:
    assert sitemap_url_candidates("https://mzh.moegirl.org.cn/sitemap/sitemap-zhmoegirl-NS_0-3.xml") == [
        "https://mzh.moegirl.org.cn/sitemap/sitemap-zhmoegirl-NS_0-3.xml",
        "https://zh.moegirl.org.cn/sitemap/sitemap-zhmoegirl-NS_0-3.xml",
        "https://mzh.moegirl.org.cn/sitemap/sitemap-zhmoegirl-NS_0-3.xml?output=1",
        "https://zh.moegirl.org.cn/sitemap/sitemap-zhmoegirl-NS_0-3.xml?output=1",
    ]
