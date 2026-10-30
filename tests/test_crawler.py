import gzip
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from crawler import crawl_pages, discover, extract_images, host_matches, open_database, parse_sitemap, write_reports


class CrawlerTests(unittest.TestCase):
    def test_social_handle_is_not_an_image(self):
        self.assertEqual(extract_images('<p>A post by @person.jpg</p><img src="@photo.jpg">', 'https://theeverygirl.com/post/', 'theeverygirl.com', {'media.theeverygirl.com'}), ['https://theeverygirl.com/post/@photo.jpg'])

    def test_everygirl_domain_and_branding(self):
        images = extract_images('<img src="/wp-content/uploads/a.jpg"><img src="https://media.theeverygirl.com/b.jpg"><img src="/wp-content/themes/theme/logo.png"><img src="https://theeverymom.com/c.jpg">', 'https://theeverygirl.com/post/', 'theeverygirl.com', {'media.theeverygirl.com'})
        self.assertEqual(images, ['https://theeverygirl.com/wp-content/uploads/a.jpg'])
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            db = open_database(output / 'crawl.sqlite3', {'sitemap': 'https://theeverygirl.com/sitemap_index.xml', 'domain': 'theeverygirl.com', 'exclude_hosts': ['media.theeverygirl.com'], 'posts_only': True})
            write_reports(db, output)
            self.assertIn('<h1>The Everygirl image audit</h1>', (output / 'report.html').read_text())
            db.close()

    def test_image_references(self):
        document = r'''<base href="https://theeverymom.com/articles/">
        <img src="/uploads/a.JPG?width=10&amp;quality=80" srcset="//theeverymom.com/b.webp 1x, https://media.theeverymom.com/c.jpg 2x">
        <img data-src="../lazy.avif"><div style="background:url('/background.png')"></div>
        <script>{"image":"https:\/\/theeverymom.com\/json.jpg?x=1\u0026y=2"}</script>
        <meta content="https://www.theeverymom.com/social.jpeg">
        <img src="https://theeverymom.com.attacker.test/evil.jpg">
        <img src="https://other.com/no.png"><img src="/not.jpg.html">
        <img src="/wp-content/themes/everymom/logo.png">
        <img src="https://www.theeverymom.com/wp-content/%74hemes/theme/font.svg">
        <script>const extensions = [".png", ".svg", ".jpg"];</script>
        <img src="/uploads/a.JPG?width=10&amp;quality=80">'''
        self.assertEqual(extract_images(document, "https://theeverymom.com/page/", "theeverymom.com", {"media.theeverymom.com"}), [
            "https://theeverymom.com/b.webp", "https://theeverymom.com/background.png",
            "https://theeverymom.com/json.jpg?x=1&y=2", "https://theeverymom.com/lazy.avif",
            "https://theeverymom.com/uploads/a.JPG?width=10&quality=80",
            "https://www.theeverymom.com/social.jpeg",
        ])

    def test_hosts(self):
        self.assertFalse(host_matches("https://media.theeverymom.com/a.png", "theeverymom.com", {"media.theeverymom.com"}))
        self.assertFalse(host_matches("https://nottheeverymom.com/a.png", "theeverymom.com", set()))
        self.assertTrue(host_matches("https://WWW.THEEVERYMOM.COM/a.png", "theeverymom.com", set()))

    def test_nested_sitemaps_and_gzip(self):
        xml = b'<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><sitemap><loc>/child.xml</loc></sitemap><sitemap><loc>/child.xml</loc></sitemap></sitemapindex>'
        self.assertEqual(parse_sitemap(gzip.compress(xml), "https://theeverymom.com/root.xml"), ("sitemapindex", {"https://theeverymom.com/child.xml": None}))

    def test_image_metadata_is_not_a_page(self):
        xml = b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" xmlns:image="http://www.google.com/schemas/sitemap-image/1.1"><url><loc>https://theeverymom.com/article/</loc><image:image><image:loc>https://theeverymom.com/a.jpg</image:loc></image:image></url></urlset>'
        self.assertEqual(parse_sitemap(xml, "https://theeverymom.com/s.xml"), ("urlset", {"https://theeverymom.com/article/": None}))

    def test_2026_posts_filter_and_report_dates(self):
        class FakeFetcher:
            def fetch(self, url):
                if url.endswith('root.xml'):
                    xml = '<sitemapindex><sitemap><loc>/post-sitemap.xml</loc></sitemap><sitemap><loc>/page-sitemap.xml</loc></sitemap></sitemapindex>'
                else:
                    xml = '<urlset><url><loc>/new/</loc><lastmod>2026-05-12T09:30:00Z</lastmod></url><url><loc>/old/</loc><lastmod>2025-01-01</lastmod></url><url><loc>/unknown/</loc></url></urlset>'
                return xml.encode(), url, 'utf-8'

        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            db = open_database(output / 'crawl.sqlite3', {'sitemap': 'https://theeverymom.com/root.xml', 'domain': 'theeverymom.com', 'exclude_hosts': [], 'year': 2026, 'posts_only': True})
            discover(db, FakeFetcher(), 'https://theeverymom.com/root.xml', year=2026, posts_only=True)
            self.assertEqual(db.execute('SELECT url,lastmod FROM pages').fetchall(), [('https://theeverymom.com/new/', '2026-05-12T09:30:00Z')])
            db.execute("INSERT INTO matches VALUES ('https://theeverymom.com/new/', 'https://theeverymom.com/photo.jpg')")
            db.commit()
            write_reports(db, output)
            report = (output / 'report.html').read_text()
            self.assertIn('<details><summary>https://theeverymom.com/new/</summary>', report)
            self.assertIn('2026-05-12T09:30:00Z', report)
            self.assertIn('lastmod', (output / 'matches.csv').read_text().splitlines()[0])
            self.assertIn('"image_count": 1', (output / 'report.json').read_text())
            db.close()
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            db = open_database(output / 'crawl.sqlite3', {'sitemap': 'https://theeverymom.com/root.xml', 'domain': 'theeverymom.com', 'exclude_hosts': [], 'years': [2021, 2022, 2023, 2024, 2025], 'posts_only': True})
            discover(db, FakeFetcher(), 'https://theeverymom.com/root.xml', year=[2021, 2022, 2023, 2024, 2025], posts_only=True)
            self.assertEqual(db.execute('SELECT url,lastmod FROM pages').fetchall(), [('https://theeverymom.com/old/', '2025-01-01')])
            write_reports(db, output)
            self.assertIn('2021, 2022, 2023, 2024, 2025', (output / 'report.html').read_text())
            db.close()
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            db = open_database(output / 'crawl.sqlite3', {'sitemap': 'https://theeverymom.com/root.xml', 'domain': 'theeverymom.com', 'exclude_hosts': [], 'exclude_years': [2025, 2026], 'posts_only': True})
            discover(db, FakeFetcher(), 'https://theeverymom.com/root.xml', posts_only=True, exclude_years=[2025, 2026])
            self.assertEqual(db.execute('SELECT url,lastmod FROM pages').fetchall(), [('https://theeverymom.com/unknown/', None)])
            db.close()

    def test_resume_and_report_coverage(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            settings = {"sitemap": "https://theeverymom.com/s.xml", "domain": "theeverymom.com", "exclude_hosts": ["media.theeverymom.com"]}
            db = open_database(output / "crawl.sqlite3", settings)
            db.execute("INSERT INTO pages (url, status) VALUES ('https://theeverymom.com/a/', 'done')")
            db.execute("INSERT INTO pages (url) VALUES ('https://theeverymom.com/b/')")
            db.execute("INSERT INTO matches VALUES ('https://theeverymom.com/a/', 'https://theeverymom.com/a.jpg')")
            db.commit()
            summary = write_reports(db, output)
            self.assertFalse(summary["complete"])
            self.assertEqual(summary["pages_pending"], 1)
            self.assertEqual(summary["page_image_pairs"], 1)
            db.close()
            db = open_database(output / "crawl.sqlite3", settings)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM matches").fetchone()[0], 1)
            db.close()

    def test_excludes_previously_saved_theme_images(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "crawl.sqlite3"
            settings = {"sitemap": "https://theeverymom.com/s.xml", "domain": "theeverymom.com", "exclude_hosts": ["media.theeverymom.com"]}
            db = open_database(path, settings)
            db.execute("INSERT INTO matches VALUES ('https://theeverymom.com/a/', 'https://theeverymom.com/wp-content/themes/theme/logo.png')")
            db.execute("INSERT INTO matches VALUES ('https://theeverymom.com/a/', 'https://theeverymom.com/wp-content/uploads/photo.jpg')")
            db.commit()
            db.close()
            db = open_database(path, settings)
            self.assertEqual(db.execute("SELECT image_url FROM matches").fetchall(), [("https://theeverymom.com/wp-content/uploads/photo.jpg",)])
            db.close()

    def test_recursive_crawl_errors_and_resume(self):
        root = "https://theeverymom.com/root.xml"
        nested = "https://theeverymom.com/nested.xml"
        leaf = "https://theeverymom.com/leaf.xml"
        page = "https://theeverymom.com/article/"
        bad = "https://theeverymom.com/fails/"

        class FakeFetcher:
            fail = True

            def __init__(self):
                self.calls = []

            def fetch(self, url):
                self.calls.append(url)
                if url == bad and self.fail:
                    raise OSError("temporary failure")
                contents = {
                    root: f"<sitemapindex><sitemap><loc>{nested}</loc></sitemap></sitemapindex>",
                    nested: f"<sitemapindex><sitemap><loc>{root}</loc></sitemap><sitemap><loc>{leaf}</loc></sitemap></sitemapindex>",
                    leaf: f"<urlset><url><loc>{page}</loc></url><url><loc>{page}</loc></url><url><loc>{bad}</loc></url></urlset>",
                    page: '<img src="/photo.jpg"><img src="https://media.theeverymom.com/excluded.jpg">',
                    bad: '<img src="/recovered.webp">',
                }
                return contents[url].encode(), url, "utf-8"

        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            settings = {"sitemap": root, "domain": "theeverymom.com", "exclude_hosts": ["media.theeverymom.com"]}
            db = open_database(output / "crawl.sqlite3", settings)
            fetcher = FakeFetcher()
            args = SimpleNamespace(workers=2, rate=4, max_pages=None, domain="theeverymom.com", exclude_host=["media.theeverymom.com"])
            discover(db, fetcher, root)
            self.assertEqual(len(fetcher.calls), 3)  # The index cycle terminates.
            crawl_pages(db, fetcher, args, output)
            first = write_reports(db, output)
            self.assertEqual(first["pages_discovered"], 2)
            self.assertEqual(first["pages_failed"], 1)
            self.assertFalse(first["complete"])
            fetcher.fail = False
            crawl_pages(db, fetcher, args, output)
            final = write_reports(db, output)
            self.assertTrue(final["complete"])
            self.assertEqual(final["page_image_pairs"], 2)
            self.assertEqual(fetcher.calls.count(page), 1)  # Successful pages are skipped.
            self.assertEqual(fetcher.calls.count(bad), 2)
            db.close()


if __name__ == "__main__":
    unittest.main()
