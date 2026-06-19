"""
fetch_star_protocols.py
Fetches STAR Protocols journal articles from PubMed Central (PMC).
CC-BY only (by/4.0 or by/3.0) — NC and ND variants are skipped.

Usage:
    python3 scripts/fetch_star_protocols.py
    python3 scripts/fetch_star_protocols.py --limit 200 --start 500
"""

import argparse
import html as _html
import json
import os
import pathlib
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import requests

# ── .env loading (same pattern as normalise.py) ───────────────────────────────
_env_path = pathlib.Path(__file__).resolve().parent.parent / ".env"
if _env_path.exists():
    for _line in _env_path.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

# ── Config ────────────────────────────────────────────────────────────────────
NCBI_API_KEY = os.environ.get("NCBI_API_KEY", "")
DELAY_SECS   = 0.11 if NCBI_API_KEY else 0.34
RAW_DIR      = pathlib.Path("data/sources/current_protocols/raw")

ESEARCH_URL  = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
EFETCH_URL   = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"

JOURNAL_TERM = "Current Protocols[Journal]"
SOURCE_NAME  = "current_protocols"

STEP_SECTION_KEYWORDS = {"step", "procedure", "protocol", "method", "before you begin", "cell culture", "preparation", "transfection", "analysis", "setting up", "flow cytometry", "extraction", "isolation", "purification", "staining", "incubation", "assembly"}
MATERIAL_SECTION_KEYWORDS = {"reagent", "material", "equipment", "key resource", "resource"}

ALI_NS = "http://www.niso.org/schemas/ali/1.0/"
XLINK_HREF = "{http://www.w3.org/1999/xlink}href"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _add_key(params: dict) -> dict:
    if NCBI_API_KEY:
        params["api_key"] = NCBI_API_KEY
    return params


def _clean(text: str) -> str:
    text = _html.unescape(text or "")
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s{2,}", " ", text)
    return text.strip()


def _iter_text(el: ET.Element) -> str:
    return _clean("".join(el.itertext()))


# ── Search / fetch ────────────────────────────────────────────────────────────

def search_pmc_paginated(limit: int, start: int) -> list:
    """Paginate ESearch until all IDs are retrieved (up to limit)."""
    ids = []
    page_size = 500
    offset = start
    print(f"Searching PMC for '{JOURNAL_TERM}' (limit={limit}, start={start})...")

    while len(ids) < limit:
        batch = min(page_size, limit - len(ids))
        params = _add_key({
            "db": "pmc",
            "term": JOURNAL_TERM,
            "retmax": batch,
            "retstart": offset,
            "retmode": "json",
        })
        page_ids = []
        total_available = 0
        for attempt in range(5):
            try:
                resp = requests.get(ESEARCH_URL, params=params, timeout=30)
                if resp.status_code == 429:
                    wait = (attempt + 1) * 10
                    print(f"  [429] Rate limit — waiting {wait}s...")
                    time.sleep(wait)
                    continue
                resp.raise_for_status()
                result = resp.json().get("esearchresult", {})
                page_ids = result.get("idlist", [])
                total_available = int(result.get("count", 0))
                break
            except Exception as exc:
                print(f"[ERROR] ESearch attempt {attempt+1} failed at offset {offset}: {exc}")
                time.sleep(5)
        if not page_ids and not total_available:
            print(f"[ERROR] ESearch gave up at offset {offset}")
            break

        if not page_ids:
            break
        ids.extend(page_ids)
        offset += len(page_ids)
        print(f"  Retrieved {len(ids)} / {min(limit, total_available)} IDs...")
        if offset >= total_available:
            break
        time.sleep(DELAY_SECS)

    return ids


def fetch_xml(pmcid: str) -> ET.Element | None:
    params = _add_key({"db": "pmc", "id": pmcid, "rettype": "xml"})
    for attempt in range(5):
        try:
            resp = requests.get(EFETCH_URL, params=params, timeout=30)
            if resp.status_code == 429:
                wait = (attempt + 1) * 10
                print(f"  [429] EFetch rate limit for PMC{pmcid} — waiting {wait}s...")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return ET.fromstring(resp.content)
        except ET.ParseError as exc:
            print(f"[ERROR] XML parse error for PMC{pmcid}: {exc}")
            return None
        except Exception as exc:
            if "429" in str(exc):
                wait = (attempt + 1) * 10
                print(f"  [429] EFetch rate limit for PMC{pmcid} — waiting {wait}s...")
                time.sleep(wait)
                continue
            print(f"[ERROR] EFetch failed for PMC{pmcid}: {exc}")
            return None
    print(f"[ERROR] EFetch gave up on PMC{pmcid} after 5 attempts")
    return None


# ── XML extraction ────────────────────────────────────────────────────────────

def extract_license(root: ET.Element) -> str:
    """Return raw license URL from PMC XML, or empty string."""
    for lic in root.findall(".//license"):
        ali_ref = lic.find(f"{{{ALI_NS}}}license_ref")
        if ali_ref is not None and ali_ref.text:
            return ali_ref.text.strip()
        href = lic.get(XLINK_HREF, "")
        if href:
            return href
        ext = lic.find(".//ext-link")
        if ext is not None:
            href = ext.get(XLINK_HREF, "")
            if href:
                return href
    ali_ref = root.find(f".//{{{ALI_NS}}}license_ref")
    if ali_ref is not None and ali_ref.text:
        return ali_ref.text.strip()
    return ""


def is_cc_by_only(license_url: str) -> tuple:
    """
    Returns (is_allowed, label).
    Only pure CC-BY (by/4.0 or by/3.0) is allowed — not NC, not ND.
    """
    if "creativecommons.org/licenses/" not in license_url:
        return False, ""
    path = license_url.split("creativecommons.org/licenses/")[-1].lower().rstrip("/")
    if re.match(r"^by/[34]\.", path):
        version = path.split("/")[1]
        return True, f"CC-BY-{version}"
    return False, ""


def is_pmc_restricted(root: ET.Element) -> bool:
    """Skip articles with <restricted-by>pmc</restricted-by>."""
    for node in root.findall(".//restricted-by"):
        if (node.text or "").strip().lower() == "pmc":
            return True
    return False


def extract_title(root: ET.Element) -> str:
    node = root.find(".//article-title")
    return _iter_text(node) if node is not None else ""


def extract_author(root: ET.Element) -> str:
    """Format as 'Surname1, Surname2, et al.' using all <surname> tags."""
    surnames = []
    for contrib in root.findall(".//contrib[@contrib-type='author']"):
        name = contrib.find("name")
        if name is not None:
            s = (name.findtext("surname") or "").strip()
            if s:
                surnames.append(s)
    if not surnames:
        return ""
    if len(surnames) == 1:
        return surnames[0]
    if len(surnames) == 2:
        return f"{surnames[0]}, {surnames[1]}"
    return f"{surnames[0]}, {surnames[1]}, et al."


def extract_doi(root: ET.Element) -> str:
    node = root.find(".//article-id[@pub-id-type='doi']")
    return (node.text or "").strip() if node is not None else ""


def extract_abstract(root: ET.Element) -> str:
    node = root.find(".//abstract")
    return _iter_text(node) if node is not None else ""


def _section_matches(title_text: str, keywords: set) -> bool:
    t = title_text.lower()
    return any(kw in t for kw in keywords)


def extract_steps_raw(root: ET.Element) -> list:
    """
    Extract steps from sections whose title contains step/procedure/protocol/method.
    Collects <p> text and ordered <list-item> text.
    Returns list of {"title": ..., "instruction": ...} dicts.
    """
    steps = []
    for sec in root.findall(".//sec"):
        title_el = sec.find("title")
        if title_el is None:
            continue
        sec_title = _iter_text(title_el)
        if not _section_matches(sec_title, STEP_SECTION_KEYWORDS):
            continue

        # Paragraphs
        for p in sec.findall("p"):
            text = _iter_text(p)
            if text:
                steps.append({"title": sec_title, "instruction": text})

        # Ordered list items
        for lst in sec.findall(".//list[@list-type='order']"):
            for item in lst.findall("list-item"):
                text = _iter_text(item)
                if text:
                    steps.append({"title": sec_title, "instruction": text})

    return steps


def extract_materials(root: ET.Element) -> list:
    """Extract list items from reagent/material/equipment sections."""
    items = []
    for sec in root.findall(".//sec"):
        title_el = sec.find("title")
        if title_el is None:
            continue
        if not _section_matches(_iter_text(title_el), MATERIAL_SECTION_KEYWORDS):
            continue
        for lst in sec.findall(".//list"):
            for item in lst.findall("list-item"):
                text = _iter_text(item)
                if text:
                    items.append(text)
        for p in sec.findall("p"):
            text = _iter_text(p)
            if text:
                items.append(text)
    return items


# ── Main ──────────────────────────────────────────────────────────────────────

def process(pmcid: str, idx: int, total: int) -> str:
    """Fetch, parse, and save one PMC article. Returns 'saved', 'skipped', or 'error'."""
    out_path = RAW_DIR / f"PMC{pmcid}.json"
    if out_path.exists():
        print(f"[{idx}/{total}] PMC{pmcid} already exists — skipping.")
        return "skipped"

    print(f"[{idx}/{total}] Fetching PMC{pmcid}...", end=" ", flush=True)
    time.sleep(DELAY_SECS)

    root = fetch_xml(pmcid)
    if root is None:
        return "error"

    license_url = extract_license(root)
    allowed, license_label = is_cc_by_only(license_url)
    if not allowed:
        print(f"license not CC-BY-only ('{license_url}') → skipped")
        return "skipped"

    title = extract_title(root)
    data = {
        "source_name": SOURCE_NAME,
        "source_id": f"PMC{pmcid}",
        "source_url": f"https://www.ncbi.nlm.nih.gov/pmc/articles/PMC{pmcid}/",
        "doi": extract_doi(root),
        "title": title,
        "author": extract_author(root),
        "license": license_label,
        "license_verified": True,
        "license_note": f"{license_label} - verified from PMC XML",
        "description": extract_abstract(root),
        "steps_raw": extract_steps_raw(root),
        "materials": extract_materials(root),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }

    try:
        RAW_DIR.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"[{license_label}] → saved ({title[:60]!r})")
        return "saved"
    except Exception as exc:
        print(f"[ERROR] Could not save PMC{pmcid}: {exc}")
        return "error"


def main():
    parser = argparse.ArgumentParser(description="Fetch Current Protocols articles from PMC")
    parser.add_argument("--limit", type=int, default=5000, help="Max articles to fetch (default 5000)")
    parser.add_argument("--start", type=int, default=0, help="ESearch offset to start from (default 0)")
    args = parser.parse_args()

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Config: DELAY={DELAY_SECS}s, API_KEY={'set' if NCBI_API_KEY else 'not set'}")

    pmc_ids = search_pmc_paginated(args.limit, args.start)
    if not pmc_ids:
        print("No PMC IDs returned. Exiting.")
        return

    saved = skipped = errors = 0
    total = len(pmc_ids)
    for idx, pmcid in enumerate(pmc_ids, start=1):
        result = process(pmcid, idx, total)
        if result == "saved":
            saved += 1
        elif result == "skipped":
            skipped += 1
        else:
            errors += 1

    print(f"\nDone. Saved: {saved} | Skipped: {skipped} | Errors: {errors}")


if __name__ == "__main__":
    main()
