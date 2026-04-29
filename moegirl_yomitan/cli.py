from __future__ import annotations

import argparse
from pathlib import Path

from .config import Settings
from .fetcher import fetch_pages
from .packaging import build_dictionary_content_fingerprint, load_last_build_fingerprint, package_dictionary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build a Yomitan dictionary from Moegirlpedia lead summaries.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    build_parser = subparsers.add_parser("build")
    add_common_arguments(build_parser)
    build_parser.add_argument(
        "--from-cache",
        action="store_true",
        help="Rebuild the dictionary archive from the existing cache without fetching new entries.",
    )

    for command in ("fetch", "package", "check-build-change"):
        subparser = subparsers.add_parser(command)
        add_common_arguments(subparser)

    return parser


def add_common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--cache-dir", type=Path, default=Settings.cache_dir)
    parser.add_argument("--output", type=Path, default=Settings.output_zip)
    parser.add_argument("--limit", type=int, default=None, help="Limit the number of discovered pages for smaller runs.")
    parser.add_argument("--summary-char-limit", type=int, default=Settings.summary_char_limit)
    parser.add_argument("--batch-size", type=int, default=Settings.batch_size)
    parser.add_argument("--concurrency", type=int, default=Settings.concurrency)
    parser.add_argument("--sitemap-concurrency", type=int, default=Settings.sitemap_concurrency)
    parser.add_argument("--chunk-size", type=int, default=Settings.chunk_size)


def settings_from_args(args: argparse.Namespace) -> Settings:
    return Settings(
        cache_dir=args.cache_dir,
        output_zip=args.output,
        summary_char_limit=args.summary_char_limit,
        batch_size=args.batch_size,
        concurrency=args.concurrency,
        sitemap_concurrency=args.sitemap_concurrency,
        chunk_size=args.chunk_size,
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    settings = settings_from_args(args)

    if args.command == "fetch":
        pages = fetch_pages(settings, limit=args.limit)
        print(f"Fetched manifest with {len(pages)} pages into {settings.cache_dir}")
        return 0

    if args.command == "package":
        output_path = package_dictionary(settings)
        print(f"Wrote dictionary archive to {output_path}")
        return 0

    if args.command == "check-build-change":
        fingerprint = build_dictionary_content_fingerprint(settings)
        previous_fingerprint = load_last_build_fingerprint(settings)
        changed = previous_fingerprint != fingerprint
        print(f"changed={'true' if changed else 'false'}")
        print(f"fingerprint={fingerprint}")
        return 0

    if args.from_cache:
        output_path = package_dictionary(settings)
        print(f"Rebuilt dictionary archive from cache: {output_path}")
        return 0

    pages = fetch_pages(settings, limit=args.limit)
    output_path = package_dictionary(settings)
    print(f"Built dictionary archive from {len(pages)} discovered pages: {output_path}")
    return 0
