#!/usr/bin/env python3
"""Daily space-news updater for alex0708mylife.

Fetches the latest headlines from NASA's official RSS feeds and rewrites
space/news.html from the template. Runs in GitHub Actions on a schedule
and can also be run locally:  python scripts/update_news.py

Design rules: headlines come straight from official sources and link back
to them - nothing is rewritten, nothing is guessed. NASA retired several
mission-specific feeds, so subjects are built by filtering the general
feeds on keywords, plus a direct feed for Artemis. If every feed fails,
the existing page is left untouched.
"""
import html
import sys
import time
from pathlib import Path

try:
    import feedparser
except ImportError:
    print("feedparser is missing - run: pip install feedparser")
    sys.exit(1)

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "scripts" / "news_template.html"
OUTPUT = ROOT / "space" / "news.html"

# (label, feed URL, optional keyword filter, max entries for the label)
# NASA retired several mission feeds in 2025-26; these are the ones that
# still serve RSS, plus ESA's space-science feed for Hubble/Webb coverage.
SOURCES = [
    ("Hubble", "https://www.nasa.gov/feed/", ["hubble"], 6),
    ("Hubble", "https://www.esa.int/rssfeed/Our_Activities/Space_Science", ["hubble"], 6),
    ("Webb", "https://www.nasa.gov/feed/", ["webb", "james webb"], 6),
    ("Webb", "https://www.esa.int/rssfeed/Our_Activities/Space_Science", ["webb", "james webb"], 6),
    ("Roman", "https://www.nasa.gov/feed/", ["roman", "nancy grace"], 6),
    ("Roman", "https://www.esa.int/rssfeed/Our_Activities/Space_Science", ["roman", "nancy grace"], 6),
    ("Artemis", "https://blogs.nasa.gov/artemis/feed/", None, 5),
    ("ISS", "https://blogs.nasa.gov/spacestation/feed/", None, 5),
    ("Space", "https://www.nasa.gov/feed/", None, 8),
]

MAX_TOTAL = 18


def fetch_feed(url):
    """Fetch one feed once, return a list of entry dicts (or [])."""
    try:
        parsed = feedparser.parse(url)
        entries = parsed.entries
        if parsed.bozo and not entries:
            print(f"  feed failed {url}: {parsed.bozo_exception}")
            return []
        out = []
        for entry in entries:
            title = html.escape(entry.get("title", "Untitled").strip())
            link = entry.get("link", "")
            if not link or not title:
                continue
            published = entry.get("published_parsed") or entry.get("updated_parsed")
            stamp = time.strftime("%d %b %Y", published) if published else ""
            out.append({"title": title, "link": link, "stamp": stamp})
        return out
    except Exception as exc:  # noqa: BLE001 - keep going on any feed failure
        print(f"  feed error {url}: {exc}")
        return []


def main():
    print("Fetching space news feeds...")

    # Fetch each unique URL once.
    cache = {}
    for _, url, _, _ in SOURCES:
        if url not in cache:
            cache[url] = fetch_feed(url)

    # Build labelled items: one dict per unique (title, link) with label set.
    items = {}
    for label, url, keywords, cap in SOURCES:
        count = 0
        for entry in cache[url]:
            if keywords and not any(k in entry["title"].lower() for k in keywords):
                continue
            key = (entry["title"], entry["link"])
            if key in items:
                items[key]["labels"].add(label)
                continue
            if count >= cap:
                break
            items[key] = {
                "title": entry["title"],
                "link": entry["link"],
                "stamp": entry["stamp"],
                "labels": {label},
            }
            count += 1
        print(f"  [{label}] {count} items from {url}")

    rows = sorted(items.values(), key=lambda r: r["stamp"], reverse=True)[:MAX_TOTAL]
    if not rows:
        print("No items fetched from any feed - leaving the existing page untouched.")
        sys.exit(0)

    lis = []
    for row in rows:
        label = " / ".join(sorted(row["labels"]))
        date = f'<span class="n-date">{row["stamp"]}</span> ' if row["stamp"] else ""
        lis.append(
            f'      <li>{date}<a href="{row["link"]}" target="_blank" rel="noopener">'
            f'{row["title"]}</a> <span class="n-src">{label}</span></li>'
        )
    body = "\n".join(lis)
    stamp = time.strftime("%d %B %Y, %H:%M UTC", time.gmtime())
    template = TEMPLATE.read_text(encoding="utf-8")
    page = template.replace("{{NEWS}}", body).replace("{{STAMP}}", stamp)
    OUTPUT.write_text(page, encoding="utf-8")
    print(f"Wrote {OUTPUT} with {len(rows)} items.")


if __name__ == "__main__":
    main()
