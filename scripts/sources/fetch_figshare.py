"""
fetch_figshare.py

Fetches CC-BY licensed lab protocols from the figshare public API.

Legal basis:
  - figshare's public API is open and does not restrict indexing of public records
  - CC-BY license confirmed per-article from the /articles/{id} endpoint
  - figshare ToS does not prohibit building derivative indexes of public, open-licensed content

Caveats:
  - figshare is primarily a dataset/supplementary material repository
  - "Protocols" are uploaded as datasets, presentations, or papers — not a dedicated type
  - Step structure lives in the description/file content; normalise.py extracts structure
  - The search endpoint returns minimal metadata; license requires a second per-article call

Flow:
  1. Search figshare articles by lab keyword (item_type=6 paper, or all types)
  2. Per-article: GET /articles/{id} — check license.name for CC-BY
  3. Reject NC/SA/ND variants
  4. Save to data/sources/figshare/raw/{article_id}.json

Usage:
    python3 scripts/sources/fetch_figshare.py
    MAX_RECORDS=500 python3 scripts/sources/fetch_figshare.py
    FIGSHARE_TOKEN=your_token python3 scripts/sources/fetch_figshare.py
"""

import json
import os
import re
import time
import pathlib
import datetime
import requests

# ── Config ────────────────────────────────────────────────────────────────────
TOKEN       = os.environ.get("FIGSHARE_TOKEN", "")
MAX_RECORDS = int(os.environ.get("MAX_RECORDS", "1000"))
PAGE_SIZE   = 100
DELAY_SEARCH = 0.3
DELAY_DETAIL = 0.4   # per-article detail call

RAW_DIR  = pathlib.Path("data/sources/figshare/raw")
SKIP_LOG = pathlib.Path("data/sources/figshare/skipped_license.jsonl")
BASE     = "https://api.figshare.com/v2"

HEADERS = {"User-Agent": "LabiApp/1.0 (mailto:support@getlabi.app)"}
if TOKEN:
    HEADERS["Authorization"] = f"token {TOKEN}"

# figshare item types: 1=figure 2=media 3=dataset 6=paper 7=presentation
# Protocols most commonly appear as dataset(3), paper(6), or presentation(7)
ITEM_TYPES = [3, 6, 7]

SEARCH_KEYWORDS = [
    "PCR protocol", "western blot protocol", "RNA extraction",
    "cell culture protocol", "ELISA assay protocol",
    "immunofluorescence protocol", "flow cytometry staining",
    "CRISPR transfection protocol", "gel electrophoresis protocol",
    "protein purification procedure", "DNA extraction protocol",
    "qPCR protocol steps", "bacterial transformation protocol",
    "cloning protocol", "microscopy imaging protocol",
]

RAW_DIR.mkdir(parents=True, exist_ok=True)
SKIP_LOG.parent.mkdir(parents=True, exist_ok=True)


def is_cc_by(license_name: str) -> bool:
    name = license_name.lower()
    return (
        "cc by" in name or "cc-by" in name or "creative commons attribution" in name
    ) and "nc" not in name and "sa" not in name and "nd" not in name


def skip(article_id, reason, title=""):
    with open(SKIP_LOG, "a") as f:
        f.write(json.dumps({"id": article_id, "reason": reason, "title": title,
                             "ts": datetime.datetime.utcnow().isoformat()}) + "\n")


def fetch_article_detail(article_id: int) -> dict | None:
    try:
        r = requests.get(f"{BASE}/articles/{article_id}", headers=HEADERS, timeout=15)
        if r.status_code == 429:
            time.sleep(30)
            r = requests.get(f"{BASE}/articles/{article_id}", headers=HEADERS, timeout=15)
        if r.status_code != 200:
            return None
        return r.json()
    except requests.RequestException:
        return None


def to_raw_schema(detail: dict) -> dict:
    description = detail.get("description", "") or ""
    description_plain = re.sub(r"<[^>]+>", " ", description).strip()
    authors = detail.get("authors", [])
    author = authors[0].get("full_name", "") if authors else ""
    license_name = detail.get("license", {}).get("name", "")
    license_url = detail.get("license", {}).get("url", "")

    return {
        "source_name": "figshare",
        "source_id": str(detail.get("id", "")),
        "source_url": detail.get("figshare_url", "") or detail.get("url_public_html", ""),
        "doi": detail.get("doi", ""),
        "title": detail.get("title", ""),
        "author": author,
        "license": license_name,
        "license_url": license_url,
        "license_verified": True,
        "license_note": f"figshare article detail endpoint license.name: {license_name}",
        "description": description_plain,
        "steps_raw": description_plain,
        "keywords": [t.get("title", "") for t in detail.get("tags", [])],
        "resource_type": detail.get("defined_type_name", ""),
        "fetched_at": datetime.datetime.utcnow().isoformat(),
    }


def fetch_keyword(keyword: str, item_type: int, saved: set, total_saved: list) -> None:
    page = 1
    while True:
        if len(total_saved) >= MAX_RECORDS:
            return
        params = {
            "search_for": keyword,
            "item_type": item_type,
            "page_size": PAGE_SIZE,
            "page": page,
        }
        try:
            r = requests.get(f"{BASE}/articles/search", params=params, headers=HEADERS, timeout=20)
            if r.status_code == 429:
                print("  Rate limited — sleeping 30s")
                time.sleep(30)
                continue
            r.raise_for_status()
        except requests.RequestException as e:
            print(f"  Error: {e}")
            break

        results = r.json()
        if not results:
            break

        for item in results:
            article_id = item.get("id")
            if not article_id or str(article_id) in saved:
                continue

            time.sleep(DELAY_DETAIL)
            detail = fetch_article_detail(article_id)
            if not detail:
                skip(article_id, "detail_fetch_failed")
                continue

            license_name = detail.get("license", {}).get("name", "")
            if not license_name:
                skip(article_id, "no_license", detail.get("title", ""))
                continue

            if not is_cc_by(license_name):
                skip(article_id, f"non_cc_by: {license_name}", detail.get("title", ""))
                continue

            raw = to_raw_schema(detail)
            out_path = RAW_DIR / f"{article_id}.json"
            out_path.write_text(json.dumps(raw, indent=2, ensure_ascii=False))
            saved.add(str(article_id))
            total_saved.append(article_id)
            print(f"  [{len(total_saved):>4}] {article_id}  {detail.get('title','')[:65]}")

            if len(total_saved) >= MAX_RECORDS:
                return

        if len(results) < PAGE_SIZE:
            break
        page += 1
        time.sleep(DELAY_SEARCH)


def main():
    saved = {p.stem for p in RAW_DIR.glob("*.json")}
    print(f"Resuming from {len(saved)} existing figshare records.")
    total_saved: list = []

    for keyword in SEARCH_KEYWORDS:
        for item_type in ITEM_TYPES:
            if len(total_saved) >= MAX_RECORDS:
                break
            print(f"\nKeyword: '{keyword}'  type={item_type}")
            fetch_keyword(keyword, item_type, saved, total_saved)
            time.sleep(DELAY_SEARCH)
        if len(total_saved) >= MAX_RECORDS:
            break

    print(f"\nDone. New records saved: {len(total_saved)}")
    print(f"Total in raw dir:  {len(list(RAW_DIR.glob('*.json')))}")


if __name__ == "__main__":
    main()
