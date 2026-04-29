# Moegirl Yomitan Builder

Build a Yomitan term dictionary from public namespace-0 萌娘百科 sitemap entries,
using only short lead summaries from the public extracts API.

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
`YYYY.MM.DD.1`, then `YYYY.MM.DD.2`, and so on. GitHub Actions computes that value from
existing git tags and publishes a matching release artifact automatically.
