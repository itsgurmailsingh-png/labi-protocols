"""
fetch_openwetware.py

Fetches lab protocols from OpenWetWare via the MediaWiki API.

⚠️  LICENSE CAVEAT — READ BEFORE RUNNING  ⚠️
  OpenWetWare uses CC-BY-SA 3.0 (Creative Commons Attribution-ShareAlike).
  This is NOT the same as CC-BY 4.0.

  What CC-BY-SA means:
    ✅ You MAY copy, redistribute, and build upon the content
    ✅ You MAY use it commercially
    ⚠️  Any DERIVATIVE WORKS must also be released under CC-BY-SA
    ⚠️  If a user modifies an OpenWetWare protocol in Labi and uploads it,
        that derivative protocol must be licensed CC-BY-SA, not CC-BY

  Decision: Accept these protocols with license="CC-BY-SA 3.0" in the schema.
  They will be stored separately from pure CC-BY sources and flagged accordingly.
  The dedup priority treats CC-BY sources higher than CC-BY-SA sources.

Legal basis:
  - OpenWetWare Terms of Use explicitly license all content under CC-BY-SA 3.0
  - MediaWiki API is publicly accessible, no ToS clause restricts indexing
  - Attribution: link back to https://openwetware.org/wiki/{page_title}

Flow:
  1. Query MediaWiki API for pages in Category:Protocols
  2. Paginate through all protocol pages
  3. Fetch page wikitext via action=parse
  4. Strip wiki markup, save structured raw JSON
  5. Dedup by page title (resume-safe)

Usage:
    python3 scripts/sources/fetch_openwetware.py
    MAX_RECORDS=500 python3 scripts/sources/fetch_openwetware.py
"""

import json
import os
import re
import time
import pathlib
import datetime
import requests

# ── Config ────────────────────────────────────────────────────────────────────
MAX_RECORDS  = int(os.environ.get("MAX_RECORDS", "3000"))
DELAY_LIST   = 0.3
DELAY_FETCH  = 0.5
BATCH_SIZE   = 500   # MediaWiki categorymembers limit

RAW_DIR  = pathlib.Path("data/sources/openwetware/raw")
SKIP_LOG = pathlib.Path("data/sources/openwetware/skipped.jsonl")
API_BASE = "https://openwetware.org/api.php"

HEADERS = {"User-Agent": "LabiApp/1.0 (mailto:support@getlabi.app)"}

# Categories to scrape — each returns a list of protocol pages
PROTOCOL_CATEGORIES = [
    "Category:Protocols",
    "Category:Lab_Protocols",
    "Category:Methods",
]

RAW_DIR.mkdir(parents=True, exist_ok=True)
SKIP_LOG.parent.mkdir(parents=True, exist_ok=True)


# ── Wiki markup stripper ───────────────────────────────────────────────────────
def strip_wiki(text: str) -> str:
    """Remove common MediaWiki markup, return plain text."""
    # Remove templates {{...}}
    while "{{" in text:
        text = re.sub(r"\{\{[^{}]*\}\}", "", text)
    # Remove [[File:...]] and [[Image:...]]
    text = re.sub(r"\[\[(File|Image):[^\]]+\]\]", "", text, flags=re.IGNORECASE)
    # Convert [[link|display]] and [[link]] to display text
    text = re.sub(r"\[\[(?:[^|\]]*\|)?([^\]]+)\]\]", r"\1", text)
    # Remove external links [http://... text] → text
    text = re.sub(r"\[https?://\S+\s+([^\]]+)\]", r"\1", text)
    text = re.sub(r"\[https?://\S+\]", "", text)
    # Headers === → plain text
    text = re.sub(r"={2,6}\s*(.*?)\s*={2,6}", r"\n\n\1\n", text)
    # Bold/italic
    text = re.sub(r"'{2,3}", "", text)
    # HTML tags
    text = re.sub(r"<[^>]+>", " ", text)
    # Collapse whitespace
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r" {2,}", " ", text)
    return text.strip()


def wiki_to_steps(text: str) -> list[dict]:
    """
    Best-effort conversion of wiki protocol text to step list.
    Splits on numbered list items or double-newlines as paragraph steps.
    """
    # Try numbered list items first (# item)
    num_items = re.findall(r"^#{1,2}\s*(.+)$", text, re.MULTILINE)
    if len(num_items) >= 2:
        return [{"step_id": i, "title": "", "instruction": s.strip(), "is_critical": False, "timers": []}
                for i, s in enumerate(num_items) if s.strip()]

    # Fall back to paragraph-based steps
    paragraphs = [p.strip() for p in text.split("\n\n") if len(p.strip()) > 20]
    return [{"step_id": i, "title": "", "instruction": p, "is_critical": False, "timers": []}
            for i, p in enumerate(paragraphs)]


def to_raw_schema(page_title: str, page_text: str) -> dict:
    plain = strip_wiki(page_text)
    steps = wiki_to_steps(plain)
    safe_id = re.sub(r"[^a-z0-9_]", "_", page_title.lower())[:80].strip("_")
    url_title = page_title.replace(" ", "_")

    return {
        "source_name": "openwetware",
        "source_id": safe_id,
        "source_url": f"https://openwetware.org/wiki/{url_title}",
        "doi": "",
        "title": page_title,
        "author": "OpenWetWare community",
        # ⚠️ CC-BY-SA 3.0 — NOT CC-BY. Derivative works must also be CC-BY-SA.
        "license": "CC-BY-SA 3.0",
        "license_verified": True,
        "license_note": (
            "OpenWetWare Terms of Use: all content licensed CC-BY-SA 3.0 "
            "(https://creativecommons.org/licenses/by-sa/3.0/). "
            "SHARE-ALIKE applies: derivative works must also be CC-BY-SA."
        ),
        "description": plain[:500],
        "steps_raw": plain,
        "steps": steps,
        "keywords": [],
        "resource_type": "protocol_wiki",
        "fetched_at": datetime.datetime.utcnow().isoformat(),
    }


# ── Fetch page list from category ─────────────────────────────────────────────
def fetch_category_members(category: str) -> list[str]:
    """Return list of page titles in a MediaWiki category."""
    titles = []
    params: dict = {
        "action": "query",
        "list": "categorymembers",
        "cmtitle": category,
        "cmlimit": BATCH_SIZE,
        "cmtype": "page",
        "format": "json",
    }
    while True:
        try:
            r = requests.get(API_BASE, params=params, headers=HEADERS, timeout=20)
            r.raise_for_status()
        except requests.RequestException as e:
            print(f"  Category fetch error: {e}")
            break

        data = r.json()
        members = data.get("query", {}).get("categorymembers", [])
        titles.extend(m["title"] for m in members)

        cont = data.get("continue", {}).get("cmcontinue")
        if not cont:
            break
        params["cmcontinue"] = cont
        time.sleep(DELAY_LIST)

    return titles


# ── Fetch page content ─────────────────────────────────────────────────────────
def fetch_page_text(title: str) -> str | None:
    """Fetch wikitext of a page via MediaWiki parse API."""
    params = {
        "action": "parse",
        "page": title,
        "prop": "wikitext",
        "format": "json",
    }
    try:
        r = requests.get(API_BASE, params=params, headers=HEADERS, timeout=20)
        r.raise_for_status()
        data = r.json()
        return data.get("parse", {}).get("wikitext", {}).get("*", "")
    except requests.RequestException as e:
        print(f"  Page fetch error for '{title}': {e}")
        return None


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    saved_ids = {p.stem for p in RAW_DIR.glob("*.json")}
    print(f"Resuming from {len(saved_ids)} existing OpenWetWare records.")
    print("⚠️  Remember: license is CC-BY-SA 3.0, not CC-BY 4.0\n")

    total_saved = []

    for category in PROTOCOL_CATEGORIES:
        if len(total_saved) >= MAX_RECORDS:
            break
        print(f"Category: {category}")
        titles = fetch_category_members(category)
        print(f"  Found {len(titles)} pages")

        for title in titles:
            if len(total_saved) >= MAX_RECORDS:
                break

            safe_id = re.sub(r"[^a-z0-9_]", "_", title.lower())[:80].strip("_")
            if safe_id in saved_ids:
                continue

            # Skip obviously non-protocol pages
            if any(skip_kw in title.lower() for skip_kw in
                   ["talk:", "user:", "oww:", "help:", "template:", "category:", "file:"]):
                continue

            time.sleep(DELAY_FETCH)
            text = fetch_page_text(title)
            if not text or len(text.strip()) < 100:
                with open(SKIP_LOG, "a") as f:
                    f.write(json.dumps({"title": title, "reason": "empty_or_short",
                                         "ts": datetime.datetime.utcnow().isoformat()}) + "\n")
                continue

            raw = to_raw_schema(title, text)
            out_path = RAW_DIR / f"{safe_id}.json"
            out_path.write_text(json.dumps(raw, indent=2, ensure_ascii=False))
            saved_ids.add(safe_id)
            total_saved.append(title)
            print(f"  [{len(total_saved):>4}] {title[:70]}")

    print(f"\nDone. New pages saved: {len(total_saved)}")
    print(f"Total in raw dir: {len(list(RAW_DIR.glob('*.json')))}")


if __name__ == "__main__":
    main()
