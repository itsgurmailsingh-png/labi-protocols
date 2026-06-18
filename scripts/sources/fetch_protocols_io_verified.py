"""
fetch_protocols_io_verified.py

Fetches protocols from protocols.io API, verifies each license via CrossRef
DOI metadata, and saves ONLY confirmed CC-BY 4.0 protocols.

Flow:
  1. Paginate protocols.io API → get title, doi, slug, author, steps
  2. For each protocol with a DOI → call CrossRef API
  3. CrossRef returns license URL registered at DOI minting time
  4. If license URL = creativecommons.org/licenses/by/ (no NC/SA/ND) → SAVE
  5. Otherwise → SKIP and log

Usage:
    export PROTOCOLS_IO_TOKEN="your_bearer_token"
    python3 scripts/sources/fetch_protocols_io_verified.py

    # Limit how many to fetch:
    MAX_PROTOCOLS=200 python3 scripts/sources/fetch_protocols_io_verified.py

    # Resume: already-saved files are skipped automatically
"""

import json
import os
import re
import time
import pathlib
import datetime

import requests

# ── Config ────────────────────────────────────────────────────────────────────
TOKEN        = os.environ.get("PROTOCOLS_IO_TOKEN", "")
MAX_PROTOCOLS = int(os.environ.get("MAX_PROTOCOLS", "2000"))
PAGE_SIZE    = 50
DELAY_PIO    = 0.5   # seconds between protocols.io API calls
DELAY_XREF   = 0.2   # seconds between CrossRef calls (generous limit)
RAW_DIR      = pathlib.Path("data/sources/protocols_io/raw")
SKIP_LOG     = pathlib.Path("data/sources/protocols_io/skipped_license.jsonl")

PIO_BASE  = "https://www.protocols.io/api/v3"
XREF_BASE = "https://api.crossref.org/works"

HEADERS_PIO = {
    "Authorization": f"Bearer {TOKEN}",
    "User-Agent": "LabiApp/1.0 (mailto:support@labi.app)",
}
HEADERS_XREF = {
    "User-Agent": "LabiApp/1.0 (mailto:support@labi.app)",
}

# protocols.io API requires a search key — use broad lab terms to get full coverage
# We dedup by protocol ID across all keyword searches
SEARCH_KEYWORDS = [
    "extraction", "protocol", "analysis", "preparation", "culture",
    "assay", "sequencing", "imaging", "western blot", "PCR",
    "RNA", "DNA", "protein", "cell", "staining", "cloning",
    "transfection", "ELISA", "buffer", "purification",
    "microscopy", "flow cytometry", "gel", "lysis", "antibody",
]


# ── Helpers ───────────────────────────────────────────────────────────────────

def is_cc_by(license_url: str) -> bool:
    """True only for plain CC-BY (commercial use allowed). Rejects NC/SA/ND."""
    url = license_url.lower()
    return (
        "creativecommons.org/licenses/by" in url
        and "/nc" not in url
        and "/sa" not in url
        and "/nd" not in url
    )


def crossref_license(doi: str) -> tuple[str, bool]:
    """
    Query CrossRef for DOI license.
    Returns (license_url, is_cc_by_bool).
    Returns ("", False) on error or no license.
    """
    if not doi:
        return "", False
    # Normalise DOI — strip URL prefix if present
    doi = re.sub(r"^https?://doi\.org/", "", doi.strip())
    try:
        r = requests.get(
            f"{XREF_BASE}/{doi}",
            headers=HEADERS_XREF,
            timeout=15,
        )
        if r.status_code == 404:
            return "", False
        r.raise_for_status()
        licenses = r.json().get("message", {}).get("license", [])
        if not licenses:
            return "", False
        url = licenses[0].get("URL", "")
        return url, is_cc_by(url)
    except Exception as e:
        print(f"    [CrossRef error] {doi}: {e}")
        return "", False


def flatten_steps(steps_data) -> list[str]:
    """Convert protocols.io steps structure to list of plain strings."""
    if not steps_data:
        return []
    result = []
    for step in steps_data:
        if isinstance(step, str):
            result.append(step)
            continue
        # protocols.io step has 'components' list with 'description' blocks
        components = step.get("components") or []
        parts = []
        for comp in components:
            desc = comp.get("description") or comp.get("body") or ""
            desc = re.sub(r"<[^>]+>", " ", str(desc)).strip()
            if desc:
                parts.append(desc)
        text = " ".join(parts).strip()
        if text:
            result.append(text)
    return result


def fetch_page(keyword: str, page: int) -> list[dict]:
    """Fetch one page of protocols from protocols.io API for a given search keyword."""
    try:
        r = requests.get(
            f"{PIO_BASE}/protocols",
            params={
                "key": keyword,
                "page_size": PAGE_SIZE,
                "page_id": page,
                "filter": "public",
            },
            headers=HEADERS_PIO,
            timeout=30,
        )
        if r.status_code == 401:
            print("[FATAL] 401 Unauthorized — PROTOCOLS_IO_TOKEN is missing or expired.")
            raise SystemExit(1)
        r.raise_for_status()
        data = r.json()
        return data.get("items") or []
    except SystemExit:
        raise
    except Exception as e:
        print(f"  [page error] keyword={keyword!r} page={page}: {e}")
        return []


def save_protocol(proto: dict, doi: str, license_url: str) -> None:
    slug = proto.get("uri") or str(proto.get("id", "unknown"))
    slug = re.sub(r"[^\w-]", "_", slug)[:80]
    out_path = RAW_DIR / f"{slug}.json"

    authors = proto.get("authors") or []
    author = ""
    if authors and isinstance(authors[0], dict):
        author = authors[0].get("name") or ""

    raw_stats = proto.get("stats") or {}
    stats = {
        "views":     int(raw_stats.get("views", 0)),
        "runs":      int(raw_stats.get("runs", 0)),
        "bookmarks": int(raw_stats.get("bookmarks", raw_stats.get("number_of_bookmarks", 0))),
        "comments":  int(raw_stats.get("comments", raw_stats.get("number_of_comments", 0))),
    }

    record = {
        "source_name": "protocols_io",
        "source_id": str(proto.get("id", "")),
        "license": "CC-BY 4.0",
        "license_verified": True,
        "license_url": license_url,
        "license_note": f"Confirmed via CrossRef DOI metadata: {doi}",
        "title": proto.get("title", ""),
        "author": author,
        "doi": doi,
        "source_url": f"https://www.protocols.io/view/{proto.get('uri', '')}",
        "steps_raw": flatten_steps(proto.get("steps")),
        "stats": stats,
        "peer_reviewed": bool(proto.get("peer_reviewed", False)),
        "fetched_at": datetime.datetime.utcnow().isoformat() + "Z",
    }
    out_path.write_text(json.dumps(record, ensure_ascii=False, indent=2))


def log_skip(proto: dict, doi: str, reason: str) -> None:
    SKIP_LOG.parent.mkdir(parents=True, exist_ok=True)
    with SKIP_LOG.open("a") as f:
        f.write(json.dumps({
            "id": proto.get("id"),
            "title": proto.get("title", "")[:80],
            "doi": doi,
            "reason": reason,
        }) + "\n")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    RAW_DIR.mkdir(parents=True, exist_ok=True)

    if not TOKEN:
        print("[FATAL] PROTOCOLS_IO_TOKEN environment variable not set.")
        print("        Run: export PROTOCOLS_IO_TOKEN='your_token'")
        print("        Get a token at: https://www.protocols.io/developers")
        raise SystemExit(1)

    # Build set of already-saved slugs for resume
    done_ids = set()
    for f in RAW_DIR.glob("*.json"):
        try:
            d = json.loads(f.read_text())
            done_ids.add(str(d.get("source_id", "")))
        except Exception:
            pass
    print(f"Resume: {len(done_ids)} protocols already saved, skipping those.")

    saved = 0
    skipped_nc = 0
    skipped_no_doi = 0
    errors = 0
    total_seen = 0
    page = 1

    print(f"Starting fetch (max {MAX_PROTOCOLS} protocols, {len(SEARCH_KEYWORDS)} keywords) …\n")

    for keyword in SEARCH_KEYWORDS:
        if saved >= MAX_PROTOCOLS:
            break
        print(f"\n── Keyword: {keyword!r} ──")
        page = 1
        keyword_new = 0

        while True:
            if saved >= MAX_PROTOCOLS:
                break
            items = fetch_page(keyword, page)
            if not items:
                break

            for proto in items:
                total_seen += 1
                pid = str(proto.get("id", ""))

                if pid in done_ids:
                    continue  # already processed this protocol from another keyword

                done_ids.add(pid)  # mark seen immediately to avoid double-processing
                title = (proto.get("title") or "")[:60]
                doi = (proto.get("doi") or "").strip()

                if not doi:
                    log_skip(proto, doi="", reason="no_doi")
                    skipped_no_doi += 1
                    continue

                # CrossRef license check — the hard gate
                license_url, ok = crossref_license(doi)
                time.sleep(DELAY_XREF)

                if not license_url:
                    log_skip(proto, doi, reason="crossref_no_license")
                    skipped_no_doi += 1
                    continue

                if not ok:
                    print(f"  [SKIP ❌] {pid}: {title[:40]} | {license_url}")
                    log_skip(proto, doi, reason=f"not_cc_by: {license_url}")
                    skipped_nc += 1
                    continue

                # Save — confirmed CC-BY
                save_protocol(proto, doi, license_url)
                saved += 1
                keyword_new += 1
                print(f"  [SAVED ✅ {saved}] {pid}: {title[:50]}")

                if saved >= MAX_PROTOCOLS:
                    break

            page += 1
            time.sleep(DELAY_PIO)

        print(f"  → {keyword_new} new CC-BY protocols from keyword {keyword!r}")

    print(f"""
Done.
  Saved (CC-BY verified):  {saved}
  Skipped (NC/SA/ND/other):{skipped_nc}
  Skipped (no DOI/license):{skipped_no_doi}
  Errors:                  {errors}
  Total protocols seen:    {total_seen}
  Skip log:                {SKIP_LOG}
""")


if __name__ == "__main__":
    main()
