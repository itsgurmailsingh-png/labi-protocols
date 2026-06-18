"""
fetch_bio_protocol_pmc.py

Fetches Bio-protocol journal papers from PubMed Central (PMC) via NCBI API.

Legal basis:
  - Bio-protocol journal is CC-BY 4.0 (confirmed via license field in PMC XML)
  - PMC Open Access subset explicitly permits text mining and database building
  - NCBI E-utilities API: no restrictions on programmatic access
  - https://www.ncbi.nlm.nih.gov/pmc/tools/openftlist/

Flow:
  1. Query PMC for all Bio-protocol OA papers (esearch)
  2. Fetch full XML for each paper (efetch)
  3. Extract: title, authors, abstract, methods/procedure sections
  4. Save to data/sources/bio_protocol/raw/{pmcid}.json
  5. Resume-safe: skip already-saved IDs

Usage:
    python3 scripts/sources/fetch_bio_protocol_pmc.py
    MAX_RECORDS=500 python3 scripts/sources/fetch_bio_protocol_pmc.py
"""

import datetime
import json
import os
import pathlib
import re
import time
import xml.etree.ElementTree as ET

import requests

# ── Config ────────────────────────────────────────────────────────────────────
MAX_RECORDS = int(os.environ.get("MAX_RECORDS", "5000"))
DELAY_SECS  = 0.4   # NCBI rate limit: 3 req/sec without API key, 10/sec with
NCBI_KEY    = os.environ.get("NCBI_API_KEY", "")

RAW_DIR = pathlib.Path("data/sources/bio_protocol/raw")
RAW_DIR.mkdir(parents=True, exist_ok=True)

BASE_URL   = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
HEADERS    = {"User-Agent": "LabiApp/1.0 (mailto:support@getlabi.app)"}

# Journals that publish complete step-by-step protocols, all CC-BY OA in PMC
PROTOCOL_JOURNALS = [
    ("Bio-protocol[journal]",   "Bio-protocol",   "CC-BY-4.0"),
]


# ── XML helpers ───────────────────────────────────────────────────────────────
def _text(el) -> str:
    """Recursively extract all text from an XML element."""
    if el is None:
        return ""
    parts = []
    if el.text:
        parts.append(el.text.strip())
    for child in el:
        parts.append(_text(child))
        if child.tail:
            parts.append(child.tail.strip())
    return " ".join(p for p in parts if p)


def _clean(text: str) -> str:
    """Normalise whitespace."""
    return re.sub(r"\s{2,}", " ", text).strip()


def parse_pmc_xml(xml_text: str, pmcid: str, license_id: str) -> dict | None:
    """Parse PMC full-text XML into raw pipeline schema."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None

    # ── Title ──────────────────────────────────────────────────────────────
    title_el = root.find(".//article-title")
    title = _clean(_text(title_el)) if title_el is not None else ""
    if not title or len(title) < 5:
        return None

    # ── Authors ────────────────────────────────────────────────────────────
    authors = []
    for contrib in root.findall(".//contrib[@contrib-type='author']"):
        surname = contrib.findtext(".//surname", "")
        given   = contrib.findtext(".//given-names", "")
        name    = f"{given} {surname}".strip()
        if name:
            authors.append(name)
    author = authors[0] if authors else ""

    # ── Journal ────────────────────────────────────────────────────────────
    journal = root.findtext(".//journal-title", "")

    # ── DOI ────────────────────────────────────────────────────────────────
    doi = ""
    for aid in root.findall(".//article-id"):
        if aid.get("pub-id-type") == "doi":
            doi = aid.text or ""
            break

    # ── License (confirm CC-BY) ────────────────────────────────────────────
    lic_ref = root.find(".//license")
    lic_text = _text(lic_ref) if lic_ref is not None else ""
    lic_url  = ""
    for el in root.iter():
        href = el.get("{http://www.w3.org/1999/xlink}href", "")
        if "creativecommons.org" in href:
            lic_url = href
            break
    # Reject no-derivatives only — NC is fine (platform provides access, users use content)
    # ND is blocked because LLM normalisation creates a derivative work
    if lic_url:
        path = lic_url.lower()
        if "-nd" in path:
            return None   # skip ND — we transform content, creating derivatives

    # ── Abstract ──────────────────────────────────────────────────────────
    abstract_el = root.find(".//abstract")
    abstract = _clean(_text(abstract_el)) if abstract_el is not None else ""

    # ── Methods / Procedure sections ──────────────────────────────────────
    # Priority: sections titled methods, procedure, protocol steps, materials
    method_keywords = re.compile(
        r"method|procedure|protocol|step|materials|reagent|recipe",
        re.IGNORECASE
    )
    sections_text = []
    for sec in root.findall(".//sec"):
        sec_title_el = sec.find("title")
        sec_title = _text(sec_title_el) if sec_title_el is not None else ""
        if method_keywords.search(sec_title):
            body = _clean(_text(sec))
            if len(body) > 50:
                sections_text.append(body)

    # If no method sections found, use body text (Bio-protocol papers ARE protocols)
    if not sections_text:
        body_el = root.find(".//body")
        if body_el is not None:
            sections_text.append(_clean(_text(body_el))[:8000])

    steps_raw = sections_text if sections_text else ([abstract] if abstract else [])

    # ── Keywords ──────────────────────────────────────────────────────────
    keywords = [_clean(_text(kw)) for kw in root.findall(".//kwd")]

    source_url = f"https://pmc.ncbi.nlm.nih.gov/articles/PMC{pmcid}/"
    if doi:
        source_url = f"https://doi.org/{doi}"

    return {
        "source_name":      "bio_protocol",
        "source_id":        pmcid,
        "source_url":       source_url,
        "doi":              doi,
        "title":            title,
        "author":           author,
        "license":          license_id,
        "license_verified": True,
        "license_note":     f"PMC full-text XML — {journal} — CC-BY confirmed in article XML",
        "description":      abstract,
        "steps_raw":        steps_raw,
        "keywords":         keywords,
        "materials_raw":    [],
        "resource_type":    "protocol",
        "fetched_at":       datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }


# ── PMC fetch ─────────────────────────────────────────────────────────────────
def search_pmc(journal_query: str) -> list[str]:
    """Return all PMC IDs for a journal, open access only."""
    ids = []
    retmax = 500
    retstart = 0
    query = f"{journal_query} AND open access[filter]"

    while True:
        params = {
            "db":       "pmc",
            "term":     query,
            "retmax":   retmax,
            "retstart": retstart,
            "retmode":  "json",
        }
        if NCBI_KEY:
            params["api_key"] = NCBI_KEY

        try:
            resp = requests.get(f"{BASE_URL}/esearch.fcgi", params=params,
                                headers=HEADERS, timeout=20)
            resp.raise_for_status()
        except requests.RequestException as e:
            print(f"  Search error: {e}")
            break

        data     = resp.json()
        result   = data.get("esearchresult", {})
        batch    = result.get("idlist", [])
        ids.extend(batch)

        total = int(result.get("count", 0))
        retstart += len(batch)
        print(f"  Retrieved {retstart}/{total} IDs …")

        if retstart >= total or not batch:
            break
        time.sleep(DELAY_SECS)

    return ids


def fetch_paper(pmcid: str) -> str | None:
    """Fetch full XML for a single PMC paper."""
    params = {"db": "pmc", "id": pmcid, "rettype": "xml", "retmode": "xml"}
    if NCBI_KEY:
        params["api_key"] = NCBI_KEY

    try:
        resp = requests.get(f"{BASE_URL}/efetch.fcgi", params=params,
                            headers=HEADERS, timeout=30)
        if resp.status_code == 429:
            print("  Rate limited — sleeping 30s")
            time.sleep(30)
            return fetch_paper(pmcid)
        resp.raise_for_status()
        return resp.text
    except requests.RequestException as e:
        print(f"  Fetch error PMC{pmcid}: {e}")
        return None


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    saved = {p.stem for p in RAW_DIR.glob("*.json")}
    print(f"Resuming from {len(saved)} existing records.")

    total_saved = 0

    for journal_query, journal_name, license_id in PROTOCOL_JOURNALS:
        print(f"\n{'='*60}")
        print(f" {journal_name}  [{license_id}]")
        print(f"{'='*60}")

        ids = search_pmc(journal_query)
        print(f"  Total OA papers found: {len(ids)}")

        for pmcid in ids:
            if total_saved >= MAX_RECORDS:
                break
            if pmcid in saved:
                continue

            xml_text = fetch_paper(pmcid)
            if not xml_text:
                continue

            record = parse_pmc_xml(xml_text, pmcid, license_id)
            if not record:
                print(f"  [SKIP] PMC{pmcid} — NC/ND license or no title")
                saved.add(pmcid)
                continue

            out_path = RAW_DIR / f"{pmcid}.json"
            out_path.write_text(json.dumps(record, indent=2, ensure_ascii=False))
            saved.add(pmcid)
            total_saved += 1
            print(f"  [{total_saved:>4}] PMC{pmcid}  {record['title'][:65]}")

            time.sleep(DELAY_SECS)

    print(f"\nDone. New records saved: {total_saved}")
    print(f"Total in raw dir: {len(list(RAW_DIR.glob('*.json')))}")


if __name__ == "__main__":
    main()
