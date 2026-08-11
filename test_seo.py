import json
import struct
import unittest
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path


ROOT = Path(__file__).resolve().parent


class SeoHtmlParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.canonicals = []
        self.h1_depth = 0
        self.h1_text = []
        self.in_body = False
        self.in_json_ld = False
        self.in_script = False
        self.in_title = False
        self.json_ld = []
        self.meta = {}
        self.scripts = []
        self.title = []
        self.visible_text = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "body":
            self.in_body = True
        elif tag == "title":
            self.in_title = True
        elif tag == "h1":
            self.h1_depth += 1
        elif tag == "meta":
            key = attributes.get("name") or attributes.get("property")
            if key:
                self.meta[key] = attributes.get("content", "")
        elif tag == "link":
            rel = set(attributes.get("rel", "").split())
            if "canonical" in rel:
                self.canonicals.append(attributes.get("href", ""))
        elif tag == "script":
            self.in_script = True
            source = attributes.get("src")
            if source:
                self.scripts.append(source)
            self.in_json_ld = attributes.get("type") == "application/ld+json"

    def handle_endtag(self, tag):
        if tag == "body":
            self.in_body = False
        elif tag == "title":
            self.in_title = False
        elif tag == "h1" and self.h1_depth:
            self.h1_depth -= 1
        elif tag == "script":
            self.in_json_ld = False
            self.in_script = False

    def handle_data(self, data):
        if self.in_title:
            self.title.append(data)
        if self.h1_depth:
            self.h1_text.append(data)
        if self.in_json_ld:
            self.json_ld.append(data)
        if data.strip() and self.in_body and not self.in_script:
            self.visible_text.append(data)


def parse_html(relative_path):
    source = (ROOT / relative_path).read_text(encoding="utf-8")
    parser = SeoHtmlParser()
    parser.feed(source)
    return source, parser


def png_size(relative_path):
    with (ROOT / relative_path).open("rb") as image:
        header = image.read(24)
    if header[:8] != b"\x89PNG\r\n\x1a\n":
        raise AssertionError(f"{relative_path} is not a PNG")
    return struct.unpack(">II", header[16:24])


class SeoContractTests(unittest.TestCase):
    def test_homepage_has_consistent_brand_signals_without_runtime_rendering(self):
        source, page = parse_html("index.html")

        self.assertEqual(
            "".join(page.title).strip(),
            "MORAL SQUAD (Морал Сквад) — мафия добра",
        )
        self.assertEqual(page.canonicals, ["https://moralsqd.ru/"])
        self.assertIn("MORAL SQUAD", "".join(page.h1_text))
        visible_text = " ".join(page.visible_text)
        self.assertNotIn(
            "мы — «Морал Сквад», а коротко — moralsqd.", visible_text
        )
        self.assertIn("index", page.meta["robots"])
        self.assertIn("max-image-preview:large", page.meta["robots"])
        self.assertEqual(page.meta["og:site_name"], "MORAL SQUAD")
        self.assertEqual(page.meta["og:url"], "https://moralsqd.ru/")
        self.assertEqual(
            page.meta["og:image"],
            "https://moralsqd.ru/og-moral-squad.png",
        )
        self.assertNotIn("./support.js", page.scripts)
        self.assertNotIn("<x-dc", source)
        self.assertNotIn("<helmet", source)

    def test_homepage_structured_data_declares_real_name_variants(self):
        _, page = parse_html("index.html")
        self.assertEqual(len(page.json_ld), 1)
        graph = json.loads(page.json_ld[0])["@graph"]
        website = next(item for item in graph if item["@type"] == "WebSite")
        organization = next(item for item in graph if item["@type"] == "Organization")

        self.assertEqual(website["name"], "MORAL SQUAD")
        self.assertEqual(website["url"], "https://moralsqd.ru/")
        self.assertEqual(
            website["alternateName"],
            ["Морал Сквад", "moralsqd", "MORAL SQD"],
        )
        self.assertEqual(organization["name"], website["name"])
        self.assertEqual(
            organization["logo"]["url"],
            "https://moralsqd.ru/icon-512.png",
        )

    def test_public_and_utility_pages_have_deliberate_indexing_rules(self):
        _, privacy = parse_html("privacy/index.html")
        _, auction = parse_html("auc/index.html")
        _, inbox = parse_html("dk-video-inbox/index.html")

        self.assertEqual(privacy.canonicals, ["https://moralsqd.ru/privacy/"])
        self.assertIn("index", privacy.meta["robots"])
        self.assertEqual(auction.canonicals, ["https://moralsqd.ru/auc/"])
        self.assertIn("noindex", auction.meta["robots"])
        self.assertIn("noindex", inbox.meta["robots"])

    def test_robots_and_sitemap_expose_only_canonical_indexable_pages(self):
        robots = (ROOT / "robots.txt").read_text(encoding="utf-8")
        self.assertIn("Disallow: /api/", robots)
        self.assertIn("Sitemap: https://moralsqd.ru/sitemap.xml", robots)

        namespace = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
        sitemap = ET.parse(ROOT / "sitemap.xml")
        urls = [node.text for node in sitemap.findall("s:url/s:loc", namespace)]
        self.assertEqual(
            urls,
            ["https://moralsqd.ru/", "https://moralsqd.ru/privacy/"],
        )

    def test_social_preview_and_icons_match_declared_dimensions(self):
        self.assertEqual(png_size("og-moral-squad.png"), (1200, 630))
        self.assertEqual(png_size("icon-512.png"), (512, 512))
        self.assertEqual(png_size("icon-192.png"), (192, 192))
        self.assertEqual(png_size("apple-touch-icon.png"), (180, 180))

        manifest = json.loads((ROOT / "site.webmanifest").read_text(encoding="utf-8"))
        self.assertEqual(manifest["name"], "MORAL SQUAD")
        self.assertEqual(
            [icon["sizes"] for icon in manifest["icons"]],
            ["192x192", "512x512"],
        )


if __name__ == "__main__":
    unittest.main()
