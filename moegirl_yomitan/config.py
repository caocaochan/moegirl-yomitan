from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Union


TimeoutType = Union[float, tuple[float, float]]


@dataclass(frozen=True)
class Settings:
    sitemap_index_url: str = "https://mzh.moegirl.org.cn/sitemap/sitemap-index-zhmoegirl.xml"
    extracts_api_url: str = "https://mzh.moegirl.org.cn/api.php"
    cache_dir: Path = Path(".cache") / "moegirl-yomitan"
    output_zip: Path = Path("dist") / "moegirl-yomitan.zip"
    standalone_index_filename: str = "moegirl-yomitan-index.json"
    dictionary_title: str = "萌娘百科"
    dictionary_source_url: str = "https://mzh.moegirl.org.cn/"
    dictionary_update_index_url: str = (
        "https://github.com/caocaochan/moegirl-yomitan/releases/latest/download/moegirl-yomitan-index.json"
    )
    dictionary_update_download_url: str = (
        "https://github.com/caocaochan/moegirl-yomitan/releases/latest/download/moegirl-yomitan.zip"
    )
    summary_char_limit: int = 240
    batch_size: int = 20
    concurrency: int = 2
    min_concurrency: int = 1
    sitemap_concurrency: int = 4
    chunk_size: int = 10_000
    request_timeout: TimeoutType = (30.0, 180.0)
    retry_attempts: int = 5
    batch_retry_attempts: int = 3
    backoff_base_seconds: float = 1.0
    adaptive_backoff_cap_seconds: float = 30.0
    user_agent: str = "moegirl-yomitan-builder/0.1 (+non-commercial summary builder)"

    @property
    def manifest_path(self) -> Path:
        return self.cache_dir / "manifest.json"

    @property
    def output_index(self) -> Path:
        return self.output_zip.with_name(self.standalone_index_filename)

    @property
    def records_dir(self) -> Path:
        return self.cache_dir / "records"
