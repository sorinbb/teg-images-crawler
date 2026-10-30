#!/usr/bin/env python3
"""Recursively audit sitemap pages for image references on selected hosts."""
import argparse
import concurrent.futures
import csv
import gzip
import html
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import sqlite3
import threading
import time
import math
from datetime import datetime, timezone
from urllib.parse import unquote, urldefrag, urljoin, urlsplit
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

EXTENSIONS = {"jpg", "jpeg", "png", "gif", "webp", "avif", "svg", "ico", "bmp", "tif", "tiff", "heic", "heif", "jxl", "apng"}
# Scan attributes, inline CSS/JS, JSON, metadata, and ordinary HTML text.
IMAGE_TOKEN = re.compile(
    r'''(?:https?://|//|(?:\.{1,2})?/)?[^\s<>"'`{}()\\,;]*?\.(?:jpg|jpeg|png|gif|webp|avif|svg|ico|bmp|tiff?|heic|heif|jxl|apng)(?=$|[\s<>"'`{}()\\,;?#])(?:\?[^\s<>"'`{}()\\,;]*)?(?:\#[^\s<>"'`{}()\\,;]*)?''',
    re.I,
)


def site_title(domain):
    return {"theeverymom.com": "The Everymom", "theeverygirl.com": "The Everygirl"}.get(domain, domain)


def normalized_text(text):
    text = html.unescape(text).replace(r"\/", "/")
    return re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m[1], 16)), text)


def host_matches(url, domain, excluded):
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    return (parsed.scheme in {"http", "https"} and host not in excluded
            and (host == domain or host.endswith("." + domain)))


def excluded_image_path(url):
    return "/wp-content/themes/" in unquote(urlsplit(url).path).lower()


def image_extension_matches(url):
    filename = unquote(urlsplit(url).path).rsplit('/', 1)[-1]
    stem, separator, extension = filename.rpartition('.')
    return bool(stem and separator and extension.lower() in EXTENSIONS)


class ReferenceParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.base = None
        self.attributes = []

    def handle_starttag(self, tag, attrs):
        for key, value in attrs:
            if value:
                self.attributes.append(value)
                if tag == "base" and key == "href" and self.base is None:
                    self.base = value


def extract_images(document, page_url, domain, excluded):
    document = normalized_text(document)
    parser = ReferenceParser()
    parser.feed(document)
    base = urljoin(page_url, parser.base) if parser.base else page_url
    images = set()
    for source_index, value in enumerate([document, *parser.attributes]):
        for match in IMAGE_TOKEN.finditer(value):
            candidate = match[0].strip()
            # Social handles such as @name.jpg in captions aren't relative image URLs.
            # Keep such filenames when explicitly referenced in an HTML attribute.
            if source_index == 0 and candidate.startswith('@'):
                continue
            url = urldefrag(urljoin(base, candidate))[0]
            if (host_matches(url, domain, excluded)
                    and not excluded_image_path(url)
                    and image_extension_matches(url)):
                images.add(url)
    return sorted(images)


def parse_sitemap(body, url):
    if body.startswith(b"\x1f\x8b"):
        body = gzip.decompress(body)
    root = ET.fromstring(body)
    kind = root.tag.rsplit("}", 1)[-1]
    if kind not in {"sitemapindex", "urlset"}:
        raise ValueError(f"Unexpected sitemap root: {kind}")
    item_tag = "sitemap" if kind == "sitemapindex" else "url"
    locations = {}
    for item in root:
        if item.tag.rsplit("}", 1)[-1] != item_tag:
            continue
        location, lastmod = None, None
        # Only direct sitemap namespace children; ignore image metadata.
        for child in item:
            if child.tag in {"loc", "{http://www.sitemaps.org/schemas/sitemap/0.9}loc"} and child.text:
                location = urldefrag(urljoin(url, child.text.strip()))[0]
                if urlsplit(location).scheme not in {"http", "https"}:
                    location = None
            if child.tag in {"lastmod", "{http://www.sitemaps.org/schemas/sitemap/0.9}lastmod"} and child.text:
                lastmod = child.text.strip()
        if location:
            locations[location] = lastmod or locations.get(location)
    return kind, dict(sorted(locations.items()))


class Fetcher:
    def __init__(self, rate, timeout, retries):
        self.interval = 1 / rate
        self.timeout = timeout
        self.retries = retries
        self.lock = threading.Lock()
        self.next_request = 0

    def fetch(self, url):
        for attempt in range(self.retries + 1):
            with self.lock:
                delay = max(0, self.next_request - time.monotonic())
                self.next_request = time.monotonic() + delay + self.interval
            if delay:
                time.sleep(delay)
            try:
                request = Request(url, headers={"User-Agent": "SitemapImageAudit/1.0", "Accept-Encoding": "gzip"})
                with urlopen(request, timeout=self.timeout) as response:
                    data = response.read()
                    if data.startswith(b"\x1f\x8b"):
                        data = gzip.decompress(data)
                    return data, response.url, response.headers.get_content_charset() or "utf-8"
            except Exception as exc:
                if attempt == self.retries:
                    raise
                # Honor Retry-After seconds when the server requests a pause.
                retry_after = getattr(exc, "headers", {}).get("Retry-After", "")
                pause = max(2 ** attempt, float(retry_after) if retry_after.isdigit() else 0)
                with self.lock:
                    self.next_request = max(self.next_request, time.monotonic() + pause)


def open_database(path, settings):
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS sitemaps (url TEXT PRIMARY KEY, status TEXT DEFAULT 'pending', error TEXT);
        CREATE TABLE IF NOT EXISTS pages (url TEXT PRIMARY KEY, status TEXT DEFAULT 'pending', final_url TEXT, error TEXT);
        CREATE TABLE IF NOT EXISTS matches (page_url TEXT, image_url TEXT, PRIMARY KEY (page_url, image_url));
    """)
    if "lastmod" not in {row[1] for row in db.execute("PRAGMA table_info(pages)")}:
        db.execute("ALTER TABLE pages ADD COLUMN lastmod TEXT")
    encoded = json.dumps(settings, sort_keys=True)
    old = db.execute("SELECT value FROM metadata WHERE key='settings'").fetchone()
    if old and old[0] != encoded:
        raise ValueError("Output directory contains a crawl with different settings. Choose another --output directory.")
    db.execute("INSERT OR IGNORE INTO metadata VALUES ('settings', ?)", (encoded,))
    db.execute("INSERT OR IGNORE INTO metadata VALUES ('started_at', ?)", (datetime.now(timezone.utc).isoformat(),))
    # Apply the current path exclusion to matches saved by earlier versions.
    excluded_images = [(image,) for (image,) in db.execute("SELECT DISTINCT image_url FROM matches") if excluded_image_path(image) or not image_extension_matches(image)]
    db.executemany("DELETE FROM matches WHERE image_url=?", excluded_images)
    db.commit()
    return db


def progress_snapshot(db, output, phase, started=None, baseline=0):
    settings = json.loads(db.execute("SELECT value FROM metadata WHERE key='settings'").fetchone()[0])
    title = html.escape(site_title(settings['domain']))
    counts = dict(db.execute("SELECT status, COUNT(*) FROM pages GROUP BY status"))
    sitemaps = dict(db.execute("SELECT status, COUNT(*) FROM sitemaps GROUP BY status"))
    total = sum(counts.values())
    crawled = counts.get("done", 0) + counts.get("error", 0)
    remaining = counts.get("pending", 0)
    elapsed = time.monotonic() - started if started is not None else 0
    rate = (crawled - baseline) / elapsed if elapsed > 0 else 0
    eta_seconds = remaining / rate if rate > 0 and phase == "crawling" else None
    eta_minutes = math.ceil(eta_seconds / 60) if eta_seconds is not None else None
    eta = f"{eta_minutes // 60}h {eta_minutes % 60:02d}m" if eta_minutes is not None else "Calculating…"
    if phase in {"complete", "finished"}:
        eta = "0h 00m" if not remaining else "Resume needed"
    if phase == "discovering":
        eta = "Available after sitemap discovery"
    progress = {
        "updated_at": datetime.now(timezone.utc).isoformat(), "phase": phase,
        "pages_total": total, "pages_crawled": crawled, "pages_remaining": remaining,
        "pages_succeeded": counts.get("done", 0), "pages_failed": counts.get("error", 0),
        "percent": round(100 * crawled / total, 1) if total else 0,
        "sitemaps_total": sum(sitemaps.values()),
        "sitemaps_processed": sitemaps.get("done", 0) + sitemaps.get("error", 0),
        "sitemaps_failed": sitemaps.get("error", 0),
        "pages_per_minute": round(rate * 60, 1), "eta_seconds": eta_seconds, "eta": eta,
    }
    for name, content in [("progress.json", json.dumps(progress, indent=2))]:
        temporary = output / (name + ".tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(output / name)
    discovery_note = "Total pages is provisional while nested sitemaps are discovered." if phase == "discovering" else "ETA uses the average page completion rate in the current run."
    dashboard = f'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><meta http-equiv="refresh" content="5">
<title>Crawler progress</title><style>body{{font:16px system-ui;background:#f4f6f8;color:#152b3c;max-width:940px;margin:60px auto;padding:24px}}h1{{font-size:32px}}.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:16px}}.card{{background:white;border:1px solid #e0e7ed;border-radius:16px;padding:24px}}strong{{display:block;font-size:36px;margin:12px 0}}progress{{width:100%;height:26px;margin:24px 0}}p{{line-height:1.7}}a{{color:#175b98}}</style>
<h1>{title} · crawler progress</h1><p>Phase: <b>{phase}</b> · refreshes every 5 seconds</p>
<div class="grid"><div class="card">Pages crawled<strong>{crawled:,} / {total:,}</strong>{progress['percent']}% processed</div><div class="card">Pages remaining<strong>{remaining:,}</strong>{counts.get('error', 0):,} failed pages</div><div class="card">Estimated time remaining<strong>{eta}</strong>{progress['pages_per_minute']:g} pages/minute</div></div>
<progress max="100" value="{progress['percent']}"></progress><p>Sitemaps processed: <b>{progress['sitemaps_processed']} / {progress['sitemaps_total']}</b> · {progress['sitemaps_failed']} failed<br>{discovery_note}<br>Last update (UTC): {progress['updated_at']}</p>
<p>Excluded image paths: <code>/wp-content/themes/</code></p><p><a href="report.html">Open image report</a> · <a href="matches.csv">Download matching URLs (CSV)</a></p></html>'''
    temporary = output / "progress.html.tmp"
    temporary.write_text(dashboard, encoding="utf-8")
    temporary.replace(output / "progress.html")
    return progress


def discover(db, fetcher, root, output=None, year=None, posts_only=False, exclude_years=None):
    years = {year} if isinstance(year, int) else set(year or [])
    excluded_years = {str(value) for value in (exclude_years or [])}
    db.execute("INSERT OR IGNORE INTO sitemaps (url) VALUES (?)", (root,))
    db.execute("UPDATE sitemaps SET status='pending' WHERE status='error'")
    db.commit()
    if output:
        progress_snapshot(db, output, "discovering")
    while True:
        row = db.execute("SELECT url FROM sitemaps WHERE status='pending' ORDER BY (error IS NOT NULL), url LIMIT 1").fetchone()
        if not row:
            return
        url = row[0]
        try:
            body, final_url, _ = fetcher.fetch(url)
            kind, locations = parse_sitemap(body, final_url)
            if kind == "sitemapindex":
                db.executemany("INSERT OR IGNORE INTO sitemaps (url) VALUES (?)", ((u,) for u in locations))
            else:
                if posts_only and not re.search(r"/post-sitemap\d*\.xml(?:\.gz)?$", urlsplit(final_url).path):
                    locations = {}
                if years:
                    locations = {u: date for u, date in locations.items() if date and re.match(r"^\d{4}-\d{2}-\d{2}(?:$|T)", date) and int(date[:4]) in years}
                if excluded_years:
                    locations = {u: date for u, date in locations.items() if not date or date[:4] not in excluded_years}
                db.executemany("INSERT INTO pages (url, lastmod) VALUES (?, ?) ON CONFLICT(url) DO UPDATE SET lastmod=COALESCE(excluded.lastmod, pages.lastmod)", locations.items())
            db.execute("UPDATE sitemaps SET status='done', error=NULL WHERE url=?", (url,))
            print(f"Sitemap: {url} -> {len(locations)} {'sitemaps' if kind == 'sitemapindex' else 'pages'}", flush=True)
        except Exception as exc:
            db.execute("UPDATE sitemaps SET status='error', error=? WHERE url=?", (str(exc), url))
            print(f"Sitemap failed: {url}: {exc}", flush=True)
        db.commit()
        if output:
            progress_snapshot(db, output, "discovering")


def write_reports(db, output):
    settings = json.loads(db.execute("SELECT value FROM metadata WHERE key='settings'").fetchone()[0])
    page_counts = dict(db.execute("SELECT status, COUNT(*) FROM pages GROUP BY status"))
    sitemap_counts = dict(db.execute("SELECT status, COUNT(*) FROM sitemaps GROUP BY status"))
    rows = list(db.execute("SELECT page_url, image_url FROM matches ORDER BY page_url, image_url"))
    errors = [{"type": table, "url": url, "error": error}
              for table in ("sitemaps", "pages")
              for url, error in db.execute(f"SELECT url, error FROM {table} WHERE status='error' ORDER BY url")]
    grouped = {}
    for page, image in rows:
        grouped.setdefault(page, []).append(image)
    lastmods = dict(db.execute("SELECT url, lastmod FROM pages"))
    page_details = {page: {"lastmod": lastmods.get(page), "image_count": len(images), "images": images} for page, images in grouped.items()}
    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "started_at": db.execute("SELECT value FROM metadata WHERE key='started_at'").fetchone()[0],
        **settings,
        "excluded_image_paths": ["/wp-content/themes/"],
        "sitemaps_discovered": sum(sitemap_counts.values()), "sitemaps_succeeded": sitemap_counts.get("done", 0),
        "sitemaps_failed": sitemap_counts.get("error", 0),
        "pages_discovered": sum(page_counts.values()), "pages_succeeded": page_counts.get("done", 0),
        "pages_failed": page_counts.get("error", 0), "pages_pending": page_counts.get("pending", 0),
        "pages_with_matches": len(grouped), "page_image_pairs": len(rows),
        "unique_images": len({image for _, image in rows}),
        "complete": not errors and not page_counts.get("pending", 0) and not sitemap_counts.get("pending", 0),
    }
    with (output / "matches.csv").open("w", newline="", encoding="utf-8") as file:
        writer = csv.writer(file)
        writer.writerow(["page_url", "lastmod", "image_url"])
        writer.writerows((page, lastmods.get(page), image) for page, image in rows)
    with (output / "pages.csv").open("w", newline="", encoding="utf-8") as file:
        writer = csv.writer(file)
        writer.writerow(["page_url", "lastmod", "status", "final_url", "matching_images", "error"])
        writer.writerows(db.execute("SELECT p.url, p.lastmod, p.status, p.final_url, COUNT(m.image_url), p.error FROM pages p LEFT JOIN matches m ON m.page_url=p.url GROUP BY p.url ORDER BY p.url"))
    (output / "report.json").write_text(json.dumps({"summary": summary, "pages": page_details, "errors": errors}, indent=2), encoding="utf-8")
    escape = html.escape
    title = escape(site_title(settings['domain']))
    table_rows = []
    for page, images in grouped.items():
        links = ''.join(f'<li><a href="{escape(image, quote=True)}" target="_blank" rel="noopener noreferrer">{escape(image)}</a></li>' for image in images)
        table_rows.append(f'<tbody class="article-group"><tr><td><details><summary>{escape(page)}</summary><p><a href="{escape(page, quote=True)}" target="_blank" rel="noopener noreferrer">Open article ↗</a></p><ol>{links}</ol></details></td><td>{escape(lastmods.get(page) or "Not provided")}</td><td>{len(images)}</td></tr></tbody>')
    table_rows = '\n'.join(table_rows)
    error_rows = "".join(f"<li>{escape(e['type'])}: {escape(e['url'])} — {escape(e['error'])}</li>" for e in errors)
    report = f'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>{title} image audit</title><style>body{{font:15px system-ui;margin:32px;color:#18222d}}h1{{font-size:28px}}table{{border-collapse:collapse;width:100%;table-layout:fixed}}td,th{{padding:14px;border:1px solid #dce1e6;text-align:left;overflow-wrap:anywhere;vertical-align:top}}th{{background:#edf2f6;position:sticky;top:0}}th:first-child{{width:65%}}th:last-child{{width:70px}}a,summary{{color:#175b98}}summary{{cursor:pointer}}summary:hover{{text-decoration:underline}}li{{margin:12px 0}}input{{padding:10px;margin:16px 0;width:min(600px,90%)}}.stats{{background:#edf2f6;padding:20px;line-height:1.8}}button{{padding:10px;cursor:pointer;margin:4px}}[hidden]{{display:none}}</style>
<h1>{title} image audit</h1><div class="stats">{'Complete' if summary['complete'] else 'Incomplete'} · {summary['pages_succeeded']:,} / {summary['pages_discovered']:,} pages scanned · {len(grouped):,} pages with matches · {len(rows):,} page/image pairs · {summary['unique_images']:,} unique images<br>Target: {escape(settings['domain'])} and subdomains. Excluded hosts: {escape(', '.join(settings['exclude_hosts']))}<br>Excluded image paths: /wp-content/themes/<br>Generated: {escape(summary['generated_at'])}</div>
<p>Scope: {'Post sitemaps only' if settings.get('posts_only') else 'All sitemap pages'} · Sitemap last-modified year(s): {', '.join(map(str, settings['years'])) if settings.get('years') else settings.get('year') or 'All years'}{' · Excluded years: ' + ', '.join(map(str, settings['exclude_years'])) if settings.get('exclude_years') else ''}. Dates below are from each page's sitemap entry, not its publication date.</p>
<input id="filter" placeholder="Filter by page or image URL" aria-label="Filter results"><button id="expand">Expand all</button><button id="collapse">Collapse all</button><p>Click an article URL to expand its matching image links. Counts are unique matching images per page.</p><table><thead><tr><th>Article URL / image links</th><th>Last modified (sitemap)</th><th>Images</th></tr></thead>{table_rows}</table>
<h2>Fetch errors ({len(errors)})</h2><ul>{error_rows}</ul><script>document.querySelector('#filter').addEventListener('input',e=>{{const q=e.target.value.toLowerCase();document.querySelectorAll('.article-group').forEach(r=>{{r.hidden=!r.textContent.toLowerCase().includes(q);if(q&&!r.hidden)r.querySelector('details').open=true}})}});document.querySelector('#expand').onclick=()=>document.querySelectorAll('.article-group:not([hidden]) details').forEach(d=>d.open=true);document.querySelector('#collapse').onclick=()=>document.querySelectorAll('details').forEach(d=>d.open=false)</script></html>'''
    (output / "report.html").write_text(report, encoding="utf-8")
    return summary


def crawl_pages(db, fetcher, args, output):
    db.execute("UPDATE pages SET status='pending' WHERE status='error'")
    db.commit()
    urls = [row[0] for row in db.execute("SELECT url FROM pages WHERE status='pending' ORDER BY url")]
    if args.max_pages is not None:
        urls = urls[:args.max_pages]
    print(f"Scanning {len(urls):,} remaining pages with {args.workers} workers, at most {args.rate:g} requests/sec", flush=True)
    started = time.monotonic()
    baseline = db.execute("SELECT COUNT(*) FROM pages WHERE status IN ('done', 'error')").fetchone()[0]
    last_progress = started
    progress_snapshot(db, output, "crawling", started, baseline)

    def scan(url):
        try:
            body, final_url, charset = fetcher.fetch(url)
            images = extract_images(body.decode(charset, errors="replace"), final_url, args.domain, set(args.exclude_host))
            return url, final_url, images, None
        except Exception as exc:
            return url, None, [], str(exc)

    # Keep only a bounded batch in flight, so interrupting a large crawl is quick.
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        iterator = iter(urls)
        pending = {pool.submit(scan, url) for url in [next(iterator, None) for _ in range(args.workers)] if url}
        count = 0
        while pending:
            done, pending = concurrent.futures.wait(pending, return_when=concurrent.futures.FIRST_COMPLETED)
            for future in done:
                url, final_url, images, error = future.result()
                db.execute("UPDATE pages SET status=?, final_url=?, error=? WHERE url=?", ("error" if error else "done", final_url, error, url))
                db.execute("DELETE FROM matches WHERE page_url=?", (url,))
                db.executemany("INSERT OR IGNORE INTO matches VALUES (?, ?)", ((url, image) for image in images))
                db.commit()
                count += 1
                if time.monotonic() - last_progress >= 5 or count == len(urls):
                    progress = progress_snapshot(db, output, "crawling", started, baseline)
                    match_count = db.execute('SELECT COUNT(*) FROM matches').fetchone()[0]
                    print(f"Crawled {progress['pages_crawled']:,}/{progress['pages_total']:,} ({progress['percent']}%) | Remaining {progress['pages_remaining']:,} | ETA {progress['eta']} | {match_count:,} image references | {progress['pages_failed']} errors", flush=True)
                    last_progress = time.monotonic()
                if count % 250 == 0:
                    write_reports(db, output)
                next_url = next(iterator, None)
                if next_url:
                    pending.add(pool.submit(scan, next_url))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sitemap", default="https://theeverymom.com/sitemap_index.xml")
    parser.add_argument("--domain", default="theeverymom.com")
    parser.add_argument("--exclude-host", action="append", default=None)
    parser.add_argument("--output", type=Path, default=Path("reports"))
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--rate", type=float, default=4, help="Maximum aggregate requests per second")
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--max-pages", type=int, help="Limit pages this run, for a smoke test; resume without this flag")
    parser.add_argument("--report-only", action="store_true", help="Regenerate reports from saved state without network access")
    year_options = parser.add_mutually_exclusive_group()
    year_options.add_argument("--year", type=int, help="Include only page entries whose sitemap lastmod is in this year")
    year_options.add_argument("--years", type=int, nargs="+", help="Include page entries whose sitemap lastmod is in any listed year")
    year_options.add_argument("--exclude-years", type=int, nargs="+", help="Exclude already covered lastmod years; also includes entries with no date")
    parser.add_argument("--posts-only", action="store_true", help="Include pages only from post-sitemap*.xml urlsets")
    parser.add_argument("--status", action="store_true", help="Print the latest saved progress and ETA, without network access")
    args = parser.parse_args()
    if args.status:
        path = args.output / "progress.json"
        if not path.exists():
            parser.error(f"No progress saved at {path}")
        print(path.read_text(encoding="utf-8"))
        return 0
    if args.workers < 1 or args.rate <= 0 or args.timeout <= 0 or args.retries < 0 or (args.max_pages is not None and args.max_pages < 0):
        parser.error("workers, rate, timeout must be positive; retries and max-pages must be nonnegative")
    args.domain = args.domain.lower().rstrip(".")
    args.exclude_host = sorted({h.lower().rstrip(".") for h in (args.exclude_host or ["media." + args.domain])})
    args.output.mkdir(parents=True, exist_ok=True)
    settings = {"sitemap": args.sitemap, "domain": args.domain, "exclude_hosts": args.exclude_host}
    if args.year is not None:
        settings["year"] = args.year
    if args.years:
        settings["years"] = sorted(set(args.years))
    if args.exclude_years:
        settings["exclude_years"] = sorted(set(args.exclude_years))
    if args.posts_only:
        settings["posts_only"] = True
    db = open_database(args.output / "crawl.sqlite3", settings)
    try:
        if not args.report_only:
            fetcher = Fetcher(args.rate, args.timeout, args.retries)
            db.execute("INSERT OR IGNORE INTO sitemaps (url) VALUES (?)", (args.sitemap,))
            db.commit()
            write_reports(db, args.output)
            discover(db, fetcher, args.sitemap, args.output, args.years or args.year, args.posts_only, args.exclude_years)
            write_reports(db, args.output)
            crawl_pages(db, fetcher, args, args.output)
    except KeyboardInterrupt:
        print("Interrupted. Saved results will resume on the next run.", flush=True)
    finally:
        summary = write_reports(db, args.output)
        if not args.report_only:
            progress_snapshot(db, args.output, "complete" if summary["complete"] else "finished")
        db.close()
        print(json.dumps(summary, indent=2), flush=True)
        print(f"Reports: {args.output.resolve() / 'report.html'}", flush=True)
    return 0 if summary["complete"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
