# Moegirl Yomitan Builder

Build a Yomitan term dictionary from public 萌娘百科 sitemap entries.

## CLI

```bash
python -m moegirl_yomitan build
python -m moegirl_yomitan build --from-cache
python -m moegirl_yomitan fetch
python -m moegirl_yomitan package
```

Useful options:

```bash
python -m moegirl_yomitan build --limit 100
python -m moegirl_yomitan build --batch-size 1 --concurrency 4
python -m moegirl_yomitan build --from-cache --output dist/moegirl-yomitan.zip
python -m moegirl_yomitan fetch --cache-dir .cache/moegirl-yomitan
python -m moegirl_yomitan package --output dist/moegirl-yomitan.zip
```

`--batch-size` controls how many page titles are packed into each extracts API request.
`--concurrency` controls how many batches run in parallel. If the remote wiki starts
rejecting long requests, lower `--batch-size` first.
`build --from-cache` rebuilds the Yomitan archive from the current local cache only and
does not download or refresh entries.

## Build versioning

Dictionary builds use the current date as the Yomitan `revision` in `YYYY.MM.DD` format.
If more than one build is released on the same day, the next build becomes
`YYYY.MM.DD.1`, then `YYYY.MM.DD.2`, and so on. The release version is computed from
existing git tags.

## Manual release

GitHub-hosted runners can be blocked by Moegirlpedia's Cloudflare challenge when fetching
sitemap XML. Releases are therefore built from a local or otherwise non-blocked
environment.

Refresh the local cache and check whether the packaged dictionary changed:

```bash
git fetch --force --tags
python -m moegirl_yomitan fetch --retry-attempts 8 --request-timeout 240 --backoff-base-seconds 2
python -m moegirl_yomitan check-build-change
python -c "from moegirl_yomitan.versioning import resolve_build_version; print(resolve_build_version())"
```

If `check-build-change` prints `changed=false`, no release is needed.

Package a changed build with the resolved version. In PowerShell:

```powershell
$env:MOEGIRL_YOMITAN_BUILD_VERSION="<version>"
python -m moegirl_yomitan build --from-cache --output dist/moegirl-yomitan.zip
```

In Bash:

```bash
export MOEGIRL_YOMITAN_BUILD_VERSION="<version>"
python -m moegirl_yomitan build --from-cache --output dist/moegirl-yomitan.zip
```

Publish the stable release assets with GitHub CLI:

```bash
gh release create "<version>" "dist/moegirl-yomitan.zip" "dist/moegirl-yomitan-index.json" --title "<version>" --notes "Manual Yomitan dictionary build for version <version>."
```

For Yomitan imports that can self-update, use this URL so the extension always checks the
latest release asset:

`https://github.com/caocaochan/moegirl-yomitan/releases/latest/download/moegirl-yomitan.zip`
