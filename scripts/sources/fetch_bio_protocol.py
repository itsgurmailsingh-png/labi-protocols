"""
fetch_bio_protocol.py — Source 1: bio-protocol.org fetcher
Fetches CC-BY 4.0 licensed lab protocols from bio-protocol.org and saves
each as JSON under data/sources/bio_protocol/raw/{id}.json.
"""

import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Config (override with environment variables)
# ---------------------------------------------------------------------------
MAX_PROTOCOLS = int(os.environ.get("BIO_PROTOCOL_MAX", "500"))
START_ID = int(os.environ.get("BIO_PROTOCOL_START_ID", "1"))
DELAY_SECS = float(os.environ.get("BIO_PROTOCOL_DELAY", "1.0"))
RAW_DIR = Path("data/sources/bio_protocol/raw")

BASE_URL = "https://bio-protocol.org"
HEADERS = {"User-Agent": "LabiApp-Protocol-Indexer/1.0 (research app; contact: support@labi.app)"}
STEP_RE = re.compile(r"^\d+[\.\)]\s+")


# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------

def get(session, url):
    """Fetch URL. Returns (resp, err_str) where err_str is '404' or message."""
    try:
        r = session.get(url, timeout=15, allow_redirects=True)
        if r.status_code == 404:
            return None, "404"
        r.raise_for_status()
        return r, None
    except requests.RequestException as e:
        return None, str(e)


# ---------------------------------------------------------------------------
# Sitemap discovery
# ---------------------------------------------------------------------------

def ids_from_sitemap(session):
    """Fetch protocol IDs from bio-protocol.org sitemaps.

    bio-protocol.org uses sitemap-protocol.xml which contains URLs like:
    https://bio-protocol.org/cn/bpdetail?id=7&type=0
    We extract the numeric IDs and use English equivalents:
    https://bio-protocol.org/en/bpdetail?id=7&type=0
    """
    # First try the index sitemap to find the protocol sub-sitemap
    r, err = get(session, BASE_URL + "/sitemap.xml")
    if not err and r:
        soup = BeautifulSoup(r.text, "xml")
        for loc in soup.find_all("loc"):
            url = loc.get_text(strip=True)
            if "sitemap-protocol" in url:
                r2, err2 = get(session, url)
                if not err2 and r2:
                    soup2 = BeautifulSoup(r2.text, "xml")
                    ids = []
                    for loc2 in soup2.find_all("loc"):
                        txt = loc2.get_text(strip=True)
                        if "id=" in txt:
                            m = re.search(r"id=(\d+)", txt)
                            if m:
                                ids.append(m.group(1))
                    if ids:
                        print(f"[sitemap] Found {len(ids)} protocol IDs from {url}")
                        return sorted(set(ids), key=int)
    return []


# ---------------------------------------------------------------------------
# Page parsing
# ---------------------------------------------------------------------------

def sel_text(soup, *selectors):
    for s in selectors:
        tag = soup.select_one(s)
        if tag:
            return tag.get_text(separator=" ", strip=True)
    return ""


def meta_content(soup, *names):
    for name in names:
        m = soup.find("meta", attrs={"name": name}) or soup.find("meta", attrs={"property": name})
        if m:
            return m.get("content", "")
    return ""


def extract_steps(soup):
    # Ordered lists inside procedure/step sections
    for sec in soup.find_all(["section", "div"], class_=lambda c: c and
            any(k in c.lower() for k in ("procedure", "step", "protocol"))):
        items = [li.get_text(separator=" ", strip=True) for li in sec.find_all("li")]
        if len(items) >= 2:
            return items
    # Any <ol>
    for ol in soup.find_all("ol"):
        items = [li.get_text(separator=" ", strip=True) for li in ol.find_all("li")]
        if len(items) >= 2:
            return items
    # Paragraphs starting with "1. " etc.
    return [p.get_text(separator=" ", strip=True)
            for p in soup.find_all("p") if STEP_RE.match(p.get_text(strip=True))]


def extract_doi(soup):
    for a in soup.find_all("a", href=True):
        if "doi.org" in a["href"]:
            return a["href"]
    for meta in soup.find_all("meta"):
        c = meta.get("content", "")
        if "doi.org" in c or c.startswith("10."):
            return c
    return ""


def parse_page(html, pid, url):
    soup = BeautifulSoup(html, "html.parser")
    title = sel_text(soup, "h1.protocol-title", "h1") or meta_content(soup, "og:title")
    author = sel_text(soup, ".author-name", ".authors", "[class*='author']") or meta_content(soup, "author")
    abstract = sel_text(soup, ".abstract", "#abstract", "[class*='abstract']") or meta_content(soup, "description", "og:description")
    category = sel_text(soup, ".category", ".section-tag", "[class*='category']") or meta_content(soup, "category")
    pub_date = sel_text(soup, "time", ".publish-date", ".date") or meta_content(soup, "date", "article:published_time")
    return {
        "source_name": "bio_protocol",
        "source_id": pid,
        "license": "CC-BY 4.0",
        "license_verified": True,
        "license_note": "bio-protocol.org publishes all protocols under CC-BY 4.0",
        "title": title,
        "author": author,
        "abstract": abstract,
        "steps_raw": extract_steps(soup),
        "doi": extract_doi(soup),
        "source_url": url,
        "category": category,
        "publish_date": pub_date,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# Fetch + save one protocol
# ---------------------------------------------------------------------------

def fetch_one(session, pid):
    out = RAW_DIR / f"{pid}.json"
    if out.exists():
        return "skipped"
    # bio-protocol.org uses query-string URLs; English version
    url = f"{BASE_URL}/en/bpdetail?id={pid}&type=0"
    r, err = get(session, url)
    if err == "404":
        return "skipped"
    if err:
        print(f"[error] {pid}: {err}")
        return "error"
    # Check for WAF block (status 468 / SafeLine WAF response)
    if r.status_code not in (200,):
        print(f"[warn] {pid}: HTTP {r.status_code} (WAF/block?) — skipping")
        return "error"
    data = parse_page(r.text, pid, url)
    if not data["title"]:
        print(f"[warn] {pid}: no title extracted — page may be WAF-blocked")
        return "error"
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[saved] {pid}: {data['title'][:80]}")
    return "saved"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    session.headers.update(HEADERS)

    ids = ids_from_sitemap(session)
    if ids:
        ids = [i for i in ids if int(i) >= START_ID][:MAX_PROTOCOLS]
    else:
        print("[info] No sitemap — using sequential ID probing")
        ids = [str(i) for i in range(START_ID, START_ID + MAX_PROTOCOLS)]

    print(f"[info] Processing up to {len(ids)} protocol IDs")
    counts = {"saved": 0, "skipped": 0, "error": 0}

    for pid in ids:
        result = fetch_one(session, pid)
        counts[result] += 1
        if result != "skipped":
            time.sleep(DELAY_SECS)

    print(f"\n[done] fetched={counts['saved']}  skipped={counts['skipped']}  errors={counts['error']}")


if __name__ == "__main__":
    main()
