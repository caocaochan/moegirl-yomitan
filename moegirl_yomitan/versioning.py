from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
import os
import re
import subprocess
from typing import Iterable, Mapping


BUILD_VERSION_ENV_VAR = "MOEGIRL_YOMITAN_BUILD_VERSION"
BUILD_VERSION_PATTERN = re.compile(r"^(?P<build_date>\d{4}\.\d{2}\.\d{2})(?:\.(?P<sequence>[1-9]\d*))?$")


def resolve_build_version(
    *,
    env: Mapping[str, str] | None = None,
    today: date | None = None,
    existing_versions: Iterable[str] | None = None,
    repo_root: Path | None = None,
) -> str:
    active_env = os.environ if env is None else env
    explicit_version = active_env.get(BUILD_VERSION_ENV_VAR)
    if explicit_version:
        return explicit_version

    known_versions = list(existing_versions) if existing_versions is not None else load_git_build_versions(repo_root=repo_root)
    return next_build_version(existing_versions=known_versions, today=today)


def next_build_version(*, existing_versions: Iterable[str], today: date | None = None) -> str:
    build_date = format_build_date(today=today)
    highest_sequence = -1

    for version in existing_versions:
        match = BUILD_VERSION_PATTERN.fullmatch(version.strip())
        if match is None or match.group("build_date") != build_date:
            continue

        sequence = int(match.group("sequence") or "0")
        highest_sequence = max(highest_sequence, sequence)

    if highest_sequence < 0:
        return build_date
    if highest_sequence == 0:
        return f"{build_date}.1"
    return f"{build_date}.{highest_sequence + 1}"


def format_build_date(*, today: date | None = None) -> str:
    active_date = datetime.now().date() if today is None else today
    return active_date.strftime("%Y.%m.%d")


def load_git_build_versions(*, repo_root: Path | None = None) -> list[str]:
    cwd = repo_root or Path.cwd()
    try:
        result = subprocess.run(
            ["git", "tag", "--list"],
            capture_output=True,
            check=False,
            cwd=cwd,
            text=True,
        )
    except OSError:
        return []

    if result.returncode != 0:
        return []

    versions: list[str] = []
    for raw_tag in result.stdout.splitlines():
        tag = raw_tag.strip()
        if BUILD_VERSION_PATTERN.fullmatch(tag):
            versions.append(tag)
            continue

        if tag.startswith("v") and BUILD_VERSION_PATTERN.fullmatch(tag[1:]):
            versions.append(tag[1:])

    return versions
