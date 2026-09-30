from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
from typing import Union


TimeoutType = Union[float, tuple[float, float]]
MAX_EXTRACT_BATCH_SIZE = 20


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
    concurrency: int = 4
    min_concurrency: int = 1
    sitemap_concurrency: int = 4
    chunk_size: int = 10_000
    request_timeout: TimeoutType = (30.0, 180.0)
    retry_attempts: int = 5
    batch_retry_attempts: int = 3
    backoff_base_seconds: float = 1.0
    adaptive_backoff_cap_seconds: float = 30.0
    user_agent: str = "moegirl-yomitan-builder/0.1 (+non-commercial summary builder)"

    def __post_init__(self) -> None:
        for name in (
            "summary_char_limit", "batch_size", "concurrency", "min_concurrency",
            "sitemap_concurrency", "chunk_size", "retry_attempts", "batch_retry_attempts",
        ):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if self.batch_size > MAX_EXTRACT_BATCH_SIZE:
            raise ValueError(f"batch_size must be at most {MAX_EXTRACT_BATCH_SIZE}")
        if self.min_concurrency > self.concurrency:
            raise ValueError("min_concurrency must not exceed concurrency")
        for name in ("backoff_base_seconds", "adaptive_backoff_cap_seconds"):
            self._validate_positive_float(name, getattr(self, name))
        timeouts = self.request_timeout if isinstance(self.request_timeout, tuple) else (self.request_timeout,)
        if isinstance(self.request_timeout, tuple) and len(timeouts) != 2:
            raise ValueError("request_timeout must be a number or a (connect, read) pair")
        for timeout in timeouts:
            self._validate_positive_float("request_timeout", timeout)
        if self.output_zip == self.output_index:
            raise ValueError("output_zip and output_index must have different paths")

    @staticmethod
    def _validate_positive_float(name: str, value: float) -> None:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise ValueError(f"{name} must be finite and greater than 0")

    @property
    def manifest_path(self) -> Path:
        return self.cache_dir / "manifest.json"

    @property
    def output_index(self) -> Path:
        return self.output_zip.with_name(self.standalone_index_filename)

    @property
    def build_state_path(self) -> Path:
        return self.cache_dir / "build-state.json"

    @property
    def record_cache_index_path(self) -> Path:
        return self.cache_dir / "record-cache-index.json"

    @property
    def negative_cache_path(self) -> Path:
        return self.cache_dir / "negative-cache.json"

    @property
    def fetch_progress_path(self) -> Path:
        return self.cache_dir / "fetch-progress.json"

    @property
    def records_dir(self) -> Path:
        return self.cache_dir / "records"
