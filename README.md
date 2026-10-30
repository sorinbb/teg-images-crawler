# teg-images-crawler

A dependency-free Python crawler for auditing image references in The Everygirl, The Everymom, or another site's sitemap pages.

It follows nested sitemaps, scans each listed page's HTML, and reports image URLs hosted on the target domain or its subdomains, excluding selected media hosts and WordPress theme assets. Reports group results by article, with sitemap last-modified dates, image counts, and expandable image links.

## Requirements and setup

- Python 3.10 or newer
- Network access to the target site
- No third-party Python packages required

```sh
git clone git@github.com:sorinbb/teg-images-crawler.git
cd teg-images-crawler
python3 crawler.py --help
```

## Crawl The Everygirl

Scan all post sitemap years, excluding `media.theeverygirl.com` and image paths under `/wp-content/themes/`:

```sh
python3 crawler.py \
  --sitemap https://theeverygirl.com/sitemap_index.xml \
  --domain theeverygirl.com \
  --exclude-host media.theeverygirl.com \
  --posts-only \
  --output reports/theeverygirl \
  --workers 8 \
  --rate 8
```

## Crawl The Everymom

```sh
python3 crawler.py \
  --posts-only \
  --output reports/theeverymom \
  --workers 8 \
  --rate 8
```

The default sitemap is `https://theeverymom.com/sitemap_index.xml`, the default domain is `theeverymom.com`, and the default excluded host is `media.<domain>`.

Without `--posts-only`, all page URLs found in the sitemaps are eligible, including category, author, and other sitemap entries.

## Open the reports

In a second terminal, serve the output locally:

```sh
python3 -m http.server 8765 --bind 127.0.0.1 --directory reports
```

For The Everygirl, open:

- Report: <http://127.0.0.1:8765/theeverygirl/report.html>
- Live progress: <http://127.0.0.1:8765/theeverygirl/progress.html>
- CSV export: <http://127.0.0.1:8765/theeverygirl/matches.csv>

Use `/theeverymom/` instead for the Everymom example above. You can also open `report.html` directly from its output folder.

Click an article URL in the report to expand its image links. Each article shows its sitemap last-modified value and count of unique matching images. Search filters by article or image URL; expand/collapse buttons control the visible rows.

## Filter by sitemap year

These filters use each page's sitemap **last-modified year**, not its publication year. An older article updated in 2026 belongs to the 2026 filter.

```sh
# One year
python3 crawler.py --posts-only --year 2026 --output reports/2026

# Several years in one report
python3 crawler.py --posts-only --years 2021 2022 2023 2024 2025 --output reports/2021-2025

# Remaining years, including entries without a last-modified date
python3 crawler.py --posts-only --exclude-years 2021 2022 2023 2024 2025 2026 --output reports/remaining-years
```

Add The Everygirl sitemap, domain, and excluded-host options to use these filters for that site. `--year`, `--years`, and `--exclude-years` are mutually exclusive. Entries without `lastmod` are excluded by the inclusion filters; otherwise they remain eligible and display “Not provided.”

## Resume, inspect, or start fresh

Progress is saved in SQLite after every completed page. Repeating a command with the same output folder and site/year filters skips successful pages and retries failures. Successful sitemaps are reused; this resumes the saved inventory rather than discovering newly published posts.

- **Fresh audit:** choose a new `--output` folder.
- **Stop:** press Ctrl+C. Reports are saved before exit; active requests may take time to finish.
- **Smoke test:** add `--max-pages 10`, then repeat the command without that limit to continue.
- **Read progress:** `python3 crawler.py --output reports/theeverygirl --status`
- **Rebuild reports:** repeat the site's command with `--report-only`; this uses saved data without network requests.

The site and year filters must match the saved crawl when resuming or using `--report-only`. The crawler rejects incompatible settings in an existing output folder.

## Output files

| File | Contents |
| --- | --- |
| `report.html` | Expandable article table, dates, image counts, coverage, and errors |
| `matches.csv` | One row per unique page/image pair: `page_url`, `lastmod`, `image_url` |
| `pages.csv` | All eligible pages, dates, fetch status, redirect destination, match count, and errors |
| `report.json` | Summary, matches grouped by page (`lastmod`, `image_count`, `images`), and errors |
| `progress.html` | Dashboard with crawled/total pages, remaining pages, failures, rate, and ETA |
| `progress.json` | Latest progress snapshot for programmatic use |
| `crawl.sqlite3` | Resumable sitemap inventory, page statuses, and matches |

Reports refresh every 250 pages and at exit. The progress dashboard refreshes every five seconds, with snapshots written as page requests finish. During discovery, page totals are provisional and ETA is unavailable. During scanning, ETA uses the current run's average completion rate.

“Crawled” includes successful and failed attempts; failures are listed separately. A 100% page count does not guarantee complete coverage if a sitemap failed. Check the report's completeness status and error list.

Generated reports, crawl databases under `reports/`, and Python cache files are excluded from Git.

## Matching rules and limits

- Follows nested sitemap indexes, handles gzip data, and deduplicates sitemap and page URLs.
- Reads image references from HTML attributes, `srcset`, lazy-loading attributes, metadata, inline CSS, inline scripts/JSON, and HTML text.
- Resolves relative URLs using the final page URL and the HTML base tag. Decodes HTML entities, escaped JSON slashes, and Unicode escapes.
- Matches the parsed target hostname or its subdomains. Excluded hosts are exact hostname matches; unrelated domains merely mentioning the target domain are excluded.
- Always excludes decoded URL paths containing `/wp-content/themes/`.
- Recognizes `jpg`, `jpeg`, `png`, `gif`, `webp`, `avif`, `svg`, `ico`, `bmp`, `tif`, `tiff`, `heic`, `heif`, `jxl`, and `apng`. Bare extensions such as `.png` without a filename are ignored.
- Ignores bare social handles such as `@name.jpg` in page text while retaining explicit attribute references to such filenames.
- Deduplicates images per page, removes URL fragments, and retains query strings.

This is a source audit. A match may come from inactive markup, placeholder data, or scripts rather than a visibly rendered image. The crawler does not execute JavaScript, fetch external CSS/JS, detect extensionless image endpoints, check image availability, or download image files. It scans sitemap-listed pages and does not follow ordinary page links or paginated pages absent from the sitemaps.

`--posts-only` recognizes URL sets named `post-sitemap.xml`, `post-sitemap2.xml`, and so on, including `.xml.gz` variants. Other sitemap naming conventions require running without that flag or adapting the filter.

## Options

| Option | Default / behavior |
| --- | --- |
| `--sitemap URL` | The Everymom sitemap index |
| `--domain HOST` | `theeverymom.com` |
| `--exclude-host HOST` | `media.<domain>`; repeat to supply multiple excluded hosts |
| `--output PATH` | `reports` |
| `--posts-only` | Restrict page entries to post sitemaps |
| `--year N`, `--years N ...` | Include selected last-modified years |
| `--exclude-years N ...` | Exclude selected last-modified years |
| `--workers N` | `4` concurrent page workers |
| `--rate N` | `4` maximum aggregate requests per second across workers |
| `--timeout SECONDS` | `30` per-request timeout |
| `--retries N` | `2` retries after the first attempt, with backoff |
| `--max-pages N` | Limit pending pages scanned in the current run |
| `--status` | Print saved progress without network access |
| `--report-only` | Regenerate reports from saved crawl data |

Exit code `0` means complete coverage of the selected sitemap entries. Exit code `2` means incomplete coverage, pending pages, or fetch errors; argument errors also use code `2`.

## Tests

```sh
python3 -m unittest discover -s tests -v
```

Tests cover nested/gzipped sitemaps, hostname and theme-path filtering, escaped image references, false-positive handling, year filters, report generation, saved-state cleanup, and resuming after fetch failures. They run without live network requests.
