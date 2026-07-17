"""
fetch_pmc_figures.py

Downloads figures for already-fetched PMC-family articles and writes a
manifest into each record's raw JSON under a new "media" field.

Covers all PMC-XML-based sources: pubmed_central, star_protocols, methodsx,
biological_procedures, current_protocols. Their raw JSON already carries
source_id="PMC{id}" but only ever kept the parsed text (see
fetch_pubmed_central.py::parse_article) — the <graphic> elements referencing
real figure files were discarded at fetch time.

Why this scrapes the article HTML instead of using PMC's official OA bulk
package: the legacy oa.fcgi service still returns a valid-looking response,
but its ftp.ncbi.nlm.nih.gov/pub/pmc/oa_package/.../PMC{id}.tar.gz links all
404 (NCBI appears to have restructured that mirror). Europe PMC's figure
render backend (ptpmcrender.fcgi) resets the connection for non-browser
clients. The one endpoint that reliably works is scraping
https://pmc.ncbi.nlm.nih.gov/articles/PMC{id}/ for its rendered
<img src="https://cdn.ncbi.nlm.nih.gov/pmc/blobs/.../{filename}"> tags and
downloading directly from that CDN — verified working via curl.

Usage:
    python3 scripts/sources/fetch_pmc_figures.py
    python3 scripts/sources/fetch_pmc_figures.py --source star_protocols
    MAX_RECORDS=500 python3 scripts/sources/fetch_pmc_figures.py
"""

import argparse
import json
import os
import pathlib
import re
import time

import requests

# ── Config ────────────────────────────────────────────────────────────────────
MAX_RECORDS = int(os.environ.get("MAX_RECORDS", "999999"))
DELAY_SECS  = 0.5

SOURCES_DIR = pathlib.Path("data/sources")
MEDIA_ROOT  = pathlib.Path("data/media")
PMC_SOURCES = [
    "pubmed_central", "star_protocols", "methodsx",
    "biological_procedures", "current_protocols",
]

ARTICLE_URL = "https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; LabiApp/1.0; mailto:support@getlabi.app)"}

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".tif", ".tiff", ".webp"}
SIZE_CAP_BYTES = 15 * 1024 * 1024

CDN_IMG_RE = re.compile(r'https://cdn\.ncbi\.nlm\.nih\.gov/pmc/blobs/[^"\'\s]+')


def sanitize_filename(name: str) -> str:
    name = re.sub(r"[^\w.\-]", "_", name)
    return name[:150] or "file"


def fetch_figure_urls(pmcid: str) -> list[str] | None:
    """Scrape the rendered article page for CDN image URLs. None on hard failure."""
    try:
        resp = requests.get(ARTICLE_URL.format(pmcid=pmcid), headers=HEADERS, timeout=20)
        if resp.status_code == 429:
            print("  Rate limited — sleeping 60s")
            time.sleep(60)
            resp = requests.get(ARTICLE_URL.format(pmcid=pmcid), headers=HEADERS, timeout=20)
        if resp.status_code == 404:
            return []
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"  [ERROR] {pmcid}: {e}")
        return None

    urls = sorted(set(CDN_IMG_RE.findall(resp.text)))
    # Drop obvious chrome (share cards, logos) — keep only figure-numbered images
    urls = [u for u in urls if pathlib.Path(u).suffix.lower() in IMAGE_EXTS]
    return urls


def download_file(url: str, dest: pathlib.Path) -> int | None:
    try:
        with requests.get(url, headers=HEADERS, timeout=30, stream=True) as r:
            r.raise_for_status()
            dest.parent.mkdir(parents=True, exist_ok=True)
            total = 0
            with open(dest, "wb") as f:
                for chunk in r.iter_content(chunk_size=65536):
                    f.write(chunk)
                    total += len(chunk)
                    if total > SIZE_CAP_BYTES:
                        f.close()
                        dest.unlink()
                        return None
        return total
    except requests.RequestException as e:
        print(f"    [ERROR] download {url}: {e}")
        if dest.exists():
            dest.unlink()
        return None


def process_record(raw_path: pathlib.Path, source_name: str) -> str:
    """Returns: 'skipped', 'no_files', 'media_saved', or 'error'."""
    try:
        raw = json.loads(raw_path.read_text())
    except Exception as e:
        print(f"  [ERROR] read {raw_path.name}: {e}")
        return "error"

    if "media" in raw:
        return "skipped"

    pmcid = raw.get("source_id") or raw_path.stem
    urls = fetch_figure_urls(pmcid)
    if urls is None:
        return "error"

    manifest = []
    for url in urls:
        filename = sanitize_filename(pathlib.Path(url).name)
        dest = MEDIA_ROOT / source_name / pmcid / filename
        downloaded_bytes = download_file(url, dest)
        if downloaded_bytes is None:
            continue
        manifest.append({
            "type": "image",
            "filename": filename,
            "local_path": str(dest),
            "source_url": url,
            "caption": "",
            "bytes": downloaded_bytes,
        })

    raw["media"] = manifest
    raw_path.write_text(json.dumps(raw, indent=2, ensure_ascii=False))

    if manifest:
        print(f"  [{source_name}/{pmcid}] {len(manifest)} figure(s) saved")
    return "media_saved" if manifest else "no_files"


def process_source(source_name: str) -> None:
    raw_dir = SOURCES_DIR / source_name / "raw"
    if not raw_dir.exists():
        print(f"[SKIP] {source_name}: no raw/ directory")
        return

    files = sorted(raw_dir.glob("*.json"))
    print(f"\n── {source_name} ── ({len(files)} records)")

    processed = saved = empty = skipped = errors = 0
    for raw_path in files:
        if processed >= MAX_RECORDS:
            break
        result = process_record(raw_path, source_name)
        if result == "skipped":
            skipped += 1
            continue
        processed += 1
        if result == "media_saved":
            saved += 1
        elif result == "no_files":
            empty += 1
        elif result == "error":
            errors += 1
        time.sleep(DELAY_SECS)

    print(f"  {source_name}: processed={processed} with_media={saved} no_media={empty} errors={errors} already_checked={skipped}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", choices=PMC_SOURCES, help="Process one source only")
    args = parser.parse_args()

    sources = [args.source] if args.source else PMC_SOURCES
    for source in sources:
        process_source(source)


if __name__ == "__main__":
    main()
