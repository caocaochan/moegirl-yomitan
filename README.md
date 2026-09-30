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
python -m moegirl_yomitan build --limit 100 --cache-dir .cache/sample --output dist/sample/moegirl-yomitan.zip
python -m moegirl_yomitan build --batch-size 1 --concurrency 4
python -m moegirl_yomitan build --from-cache --output dist/moegirl-yomitan.zip
python -m moegirl_yomitan fetch --cache-dir .cache/moegirl-yomitan
python -m moegirl_yomitan package --output dist/moegirl-yomitan.zip
```

`--batch-size` controls how many page titles are packed into each extracts API request.
`--concurrency` is the ceiling for all simultaneous entry API requests, including
listed-link lookups. Fetching starts at up to four workers (default ceiling: four)
and tunes concurrency automatically using throughput and request latency. If the remote wiki starts
rejecting long requests, lower `--batch-size` first.
`build --from-cache` rebuilds the Yomitan archive from the current local cache only and
does not download or refresh entries.

Entry downloads use resumable extract and listed-link steps. Ordinary summaries
are saved immediately; disambiguation records are saved only after their complete
HTML/link lookup succeeds. Link requests share the entry concurrency ceiling and
have a bounded queue. Full HTML and link continuation behavior is preserved,
including lists below headings.

`--retry-attempts` is the total attempt budget **per entry API step**, including the
first request and host fallback. For example, `--retry-attempts 8` permits at most
eight attempts for a failed extract or link continuation; completed steps are not
replayed. Backoff releases worker slots, uses jitter, and is capped at 30 seconds
unless `Retry-After` requires longer. HTTP 429 and MediaWiki `maxlag` pause entry
requests across both wiki host aliases. Sitemap downloads retain their previous
transport/retry behavior. The Python `Settings.batch_retry_attempts` constructor
field is deprecated and no longer affects entry fetching.

Automatic tuning halves concurrency on throttling and lowers it by one on other
transient failures. Throughput probes require two healthy 30-second windows with
at least ten successful extract requests each; a higher level is retained only
when it improves throughput by at least 10% without excessive retries or latency.
Unsuccessful probes revert and are suppressed for five minutes. Progress reports
include stage queues, active requests, delayed retries, shared cooldown, request
p50/p95, extract throughput, and finalized-entry throughput. Server caching can
strongly affect timings; increasing the ceiling does not guarantee a speedup.

On an exhausted or permanent failure, fetching stops new submissions, drains
running attempts, and checkpoints complete entries before returning an error.
Incomplete step state is memory-only and is fetched again after restarting.

A bounded live comparison of fixed four workers and adaptive ceilings of four and
eight is available with:

```bash
python scripts/benchmark_downloads.py --pages-per-case 1200 --max-seconds-per-case 120
```

It samples distinct batches balanced by cached summary length and disambiguation
prevalence, reports repeated server-cached batches separately, and writes only to
a separate `.cache/download-benchmark` directory. Increase the sample and time
limits to observe the two-minute probe evaluation; short cases may finish with
the eight-worker ceiling still operating at four workers.

`--limit` replaces the selected cache's manifest with the discovered subset. Use a
separate `--cache-dir` and an output in a separate directory for sample runs; the
standalone update index uses a fixed filename beside the archive.

Cached summaries record their extraction limit and whether the complete lead was
retained. Smaller `--summary-char-limit` values work offline without rewriting the
cache. Larger values require `fetch --summary-char-limit <limit>` when the cached
lead was truncated. Legacy records without this metadata are treated as having
the original 240-character limit; custom limits used by older versions cannot be
inferred from those records.

Malformed record files are reported and refetched. Packaging stops on damaged or
missing referenced records and on builds with no usable entries. ZIP and index
assets are staged and verified before publication; caught replacement failures
roll back the previous outputs. If rollback is blocked, the error identifies a
staging directory containing the recovery files. File replacements are atomic
individually, but a process or power interruption between them is not transactional.

The next fetch ignores old negative-cache outcomes and retries those pages once.
Successful cached records remain available. Redirect freshness is stored per
source URL so aliases can share a summary without repeated fetching.

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

If `check-build-change` prints `changed=false`, packaged content is unchanged; `release.bat`
still publishes a forced release when run.

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

The Windows release script writes release-diff HTML as UTF-8 using
`diff-releases --output <path>`. Release discovery follows all GitHub release
pages with bounded request timeouts. A first dictionary release lists all entries
as additions.

For Yomitan imports that can self-update, use this URL so the extension always checks the
latest release asset:

`https://github.com/caocaochan/moegirl-yomitan/releases/latest/download/moegirl-yomitan.zip`

## Validation

Install the development dependencies and run the offline suite:

```bash
python -m pip install -e ".[dev]"
python -m pytest -q -k "not smoke_build_and_package"
```

CI runs this suite on pull requests and pushes, on Windows/Linux with Python
3.10 and 3.12. Unit tests reject unexpected network requests. The optional
`test_smoke_build_and_package` test contacts the live wiki.

Official Yomitan schemas are vendored in `tests/fixtures/yomitan`; provenance
records the upstream commit, URLs, and SHA-256 hashes, with the upstream license.
Pinyin dependency versions participate in fingerprint-cache compatibility so an
upgrade recomputes packaged fingerprints.
