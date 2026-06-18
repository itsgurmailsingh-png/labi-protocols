"""
fetch_pubmed_central.py
Fetches lab protocols from PubMed Central (PMC) using the NCBI E-utilities API.
Source 2 in the multi-source pipeline — CC-BY licensed articles only.
"""

import json
import os
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
NCBI_API_KEY = os.environ.get("NCBI_API_KEY", "")
MAX_PROTOCOLS = int(os.environ.get("PMC_MAX", "500"))
DELAY_SECS = 0.11 if NCBI_API_KEY else 0.34
RAW_DIR = Path("data/sources/pubmed_central/raw")

ESEARCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
EFETCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"

SEARCH_QUERY = (
    '(protocol[Title] OR method[Title] OR procedure[Title])'
    ' AND open access[filter]'
    ' AND ("laboratory"[Title/Abstract] OR "cell"[Title/Abstract]'
    ' OR "RNA"[Title/Abstract] OR "protein"[Title/Abstract]'
    ' OR "PCR"[Title/Abstract] OR "assay"[Title/Abstract]'
    ' OR "extraction"[Title/Abstract] OR "staining"[Title/Abstract])'
)

METHOD_SECTION_TITLES = {"protocol", "methods", "procedure", "steps", "method"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _add_api_key(params: dict) -> dict:
    if NCBI_API_KEY:
        params["api_key"] = NCBI_API_KEY
    return params


def _xml_text(element, path: str, namespaces=None) -> str:
    """Return joined text content of all matching subelements, or empty string."""
    if element is None:
        return ""
    nodes = element.findall(path, namespaces) if namespaces else element.findall(path)
    parts = []
    for node in nodes:
        text = "".join(node.itertext()).strip()
        if text:
            parts.append(text)
    return " ".join(parts)


def search_pmc(max_results: int) -> list[str]:
    """Run ESearch and return a list of PMC IDs."""
    params = _add_api_key({
        "db": "pmc",
        "term": SEARCH_QUERY,
        "retmax": max_results,
        "retmode": "json",
    })
    try:
        resp = requests.get(ESEARCH_URL, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        ids = data.get("esearchresult", {}).get("idlist", [])
        print(f"ESearch returned {len(ids)} PMC IDs.")
        return ids
    except Exception as exc:
        print(f"[ERROR] ESearch failed: {exc}")
        return []


def fetch_article_xml(pmcid: str) -> ET.Element | None:
    """Fetch and parse full article XML from EFetch for a single PMC ID."""
    params = _add_api_key({
        "db": "pmc",
        "id": pmcid,
        "rettype": "xml",
        "retmode": "xml",
    })
    try:
        resp = requests.get(EFETCH_URL, params=params, timeout=30)
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
        return root
    except ET.ParseError as exc:
        print(f"[ERROR] XML parse error for PMC{pmcid}: {exc}")
        return None
    except Exception as exc:
        print(f"[ERROR] EFetch failed for PMC{pmcid}: {exc}")
        return None


def extract_title(root: ET.Element) -> str:
    node = root.find(".//article-title")
    if node is not None:
        return "".join(node.itertext()).strip()
    return ""


def extract_author(root: ET.Element) -> str:
    """Return 'Last, First' of first contributing author."""
    for contrib in root.findall(".//contrib[@contrib-type='author']"):
        name = contrib.find("name")
        if name is not None:
            surname = (name.findtext("surname") or "").strip()
            given = (name.findtext("given-names") or "").strip()
            parts = [p for p in [surname, given] if p]
            if parts:
                return ", ".join(parts)
    return ""


def extract_abstract(root: ET.Element) -> str:
    abstract_el = root.find(".//abstract")
    if abstract_el is not None:
        return " ".join(abstract_el.itertext()).strip()
    return ""


def extract_steps_raw(root: ET.Element) -> list[str]:
    """Collect paragraph text from sections whose title matches method-like keywords."""
    steps: list[str] = []
    for sec in root.findall(".//sec"):
        title_el = sec.find("title")
        if title_el is None:
            continue
        title_text = "".join(title_el.itertext()).strip().lower()
        if any(kw in title_text for kw in METHOD_SECTION_TITLES):
            for para in sec.findall(".//p"):
                text = "".join(para.itertext()).strip()
                if text:
                    steps.append(text)
    return steps


def extract_doi(root: ET.Element) -> str:
    for node in root.findall(".//article-id[@pub-id-type='doi']"):
        return node.text.strip() if node.text else ""
    return ""


def extract_pmc_id(root: ET.Element) -> str:
    for node in root.findall(".//article-id[@pub-id-type='pmc']"):
        return (node.text or "").strip()
    return ""


def extract_license_url(root: ET.Element) -> str:
    """
    PMC XML stores license URL in multiple possible locations:
    1. NISO ALI namespace: <ali:license_ref>https://...</ali:license_ref>
    2. xlink:href on <license> element
    3. xlink:href on nested <ext-link>
    """
    ALI_NS = "http://www.niso.org/schemas/ali/1.0/"

    for lic in root.findall(".//license"):
        # 1. NISO ALI child element (most common in modern PMC XML)
        ali_ref = lic.find(f"{{{ALI_NS}}}license_ref")
        if ali_ref is not None and ali_ref.text:
            return ali_ref.text.strip()

        # 2. xlink:href on the <license> tag itself
        href = lic.get("{http://www.w3.org/1999/xlink}href", "")
        if href:
            return href

        # 3. xlink:href on nested <ext-link>
        ext_link = lic.find(".//ext-link")
        if ext_link is not None:
            href = ext_link.get("{http://www.w3.org/1999/xlink}href", "")
            if href:
                return href

    # 4. Top-level ali:license_ref anywhere in doc
    ali_ref = root.find(f".//{{{ALI_NS}}}license_ref")
    if ali_ref is not None and ali_ref.text:
        return ali_ref.text.strip()

    return ""


def extract_publish_date(root: ET.Element) -> str:
    for pub_type in ("epub", "ppub", "collection"):
        node = root.find(f".//pub-date[@pub-type='{pub_type}']")
        if node is not None:
            year = (node.findtext("year") or "").strip()
            month = (node.findtext("month") or "").strip().zfill(2)
            day = (node.findtext("day") or "").strip().zfill(2)
            parts = [p for p in [year, month, day] if p and p != "00"]
            if parts:
                return "-".join(parts)
    return ""


def is_cc_by(license_url: str) -> bool:
    return "creativecommons.org/licenses/by/" in license_url and "/nc" not in license_url


def parse_article(root: ET.Element, pmcid: str) -> dict | None:
    """
    Extract all fields from the article XML.
    Returns None if license check fails.
    """
    license_url = extract_license_url(root)

    if not is_cc_by(license_url):
        return None

    return {
        "source_name": "pubmed_central",
        "source_id": f"PMC{pmcid}",
        "license": "CC-BY 4.0",
        "license_verified": True,
        "license_url": license_url,
        "license_note": f"Confirmed from PMC XML: {license_url}",
        "title": extract_title(root),
        "author": extract_author(root),
        "abstract": extract_abstract(root),
        "steps_raw": extract_steps_raw(root),
        "doi": extract_doi(root),
        "source_url": f"https://www.ncbi.nlm.nih.gov/pmc/articles/PMC{pmcid}/",
        "publish_date": extract_publish_date(root),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


def save_protocol(data: dict, pmcid: str) -> None:
    out_path = RAW_DIR / f"PMC{pmcid}.json"
    out_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    RAW_DIR.mkdir(parents=True, exist_ok=True)

    print(f"Config: MAX_PROTOCOLS={MAX_PROTOCOLS}, DELAY={DELAY_SECS}s, "
          f"API_KEY={'set' if NCBI_API_KEY else 'not set'}")

    pmc_ids = search_pmc(MAX_PROTOCOLS)
    if not pmc_ids:
        print("No PMC IDs returned. Exiting.")
        return

    total_fetched = 0
    total_skipped = 0
    total_errors = 0

    for i, pmcid in enumerate(pmc_ids, start=1):
        out_path = RAW_DIR / f"PMC{pmcid}.json"

        # Resume: skip if already saved
        if out_path.exists():
            print(f"[{i}/{len(pmc_ids)}] PMC{pmcid} already exists — skipping.")
            total_fetched += 1
            continue

        time.sleep(DELAY_SECS)

        root = fetch_article_xml(pmcid)
        if root is None:
            total_errors += 1
            continue

        try:
            data = parse_article(root, pmcid)
        except Exception as exc:
            print(f"[ERROR] Parsing failed for PMC{pmcid}: {exc}")
            total_errors += 1
            continue

        if data is None:
            license_url = extract_license_url(root)
            print(f"[{i}/{len(pmc_ids)}] PMC{pmcid} skipped — license not CC-BY: '{license_url}'")
            total_skipped += 1
            continue

        try:
            save_protocol(data, pmcid)
            print(f"[{i}/{len(pmc_ids)}] Saved PMC{pmcid}: {data['title'][:60]!r}")
            total_fetched += 1
        except Exception as exc:
            print(f"[ERROR] Could not save PMC{pmcid}: {exc}")
            total_errors += 1

    print(
        f"\nDone. Fetched: {total_fetched} | "
        f"Skipped (wrong license): {total_skipped} | "
        f"Errors: {total_errors}"
    )


if __name__ == "__main__":
    main()
