"""
fetch_zenodo_media.py

Downloads essential media (images, PDFs, small supplementary files) attached
to already-fetched Zenodo records, and writes a manifest into each record's
raw JSON under a new "media" field.

Many Zenodo protocol/dataset records ARE their figures (e.g. "unprocessed
Western blot images, EM images..."). fetch_zenodo.py only ever kept the text
description — this script closes that gap without re-fetching protocol text.

Flow:
  1. Read data/sources/zenodo/raw/{id}.json for every already-fetched record
  2. Skip records that already have a "media" key (resume-safe — presence of
     the key means "already checked", even if the list is empty)
  3. GET https://zenodo.org/api/records/{id} for the file listing
  4. Keep image/pdf files under SIZE_CAP; other file types only if small
     (likely a supplementary doc, not a raw dataset dump)
  5. Download to data/media/zenodo/{id}/{filename}
  6. Write the manifest into the raw JSON's "media" field (in place)

Usage:
    python3 scripts/sources/fetch_zenodo_media.py
    MAX_RECORDS=500 python3 scripts/sources/fetch_zenodo_media.py
    ZENODO_TOKEN=your_token python3 scripts/sources/fetch_zenodo_media.py  # raises rate limit
"""

import json
import os
import pathlib
import re
import time

import requests

# ── Config ────────────────────────────────────────────────────────────────────
TOKEN       = os.environ.get("ZENODO_TOKEN", "")
MAX_RECORDS = int(os.environ.get("MAX_RECORDS", "999999"))
DELAY_SECS  = 0.4   # Zenodo public rate limit: ~60 req/min unauthenticated

RAW_DIR   = pathlib.Path("data/sources/zenodo/raw")
MEDIA_DIR = pathlib.Path("data/media/zenodo")
BASE_URL  = "https://zenodo.org/api/records"

HEADERS = {"User-Agent": "LabiApp/1.0 (mailto:support@getlabi.app)"}
if TOKEN:
    HEADERS["Authorization"] = f"Bearer {TOKEN}"

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".gif", ".bmp", ".webp"}
PDF_EXTS   = {".pdf"}
SIZE_CAP_BYTES        = 15 * 1024 * 1024   # images/pdf up to 15MB
SMALL_FILE_CAP_BYTES  = 2 * 1024 * 1024    # any other file type only if <2MB

MEDIA_DIR.mkdir(parents=True, exist_ok=True)


def sanitize_filename(name: str) -> str:
    name = re.sub(r"[^\w.\-]", "_", name)
    return name[:150] or "file"


def media_type_for(filename: str) -> str | None:
    ext = pathlib.Path(filename).suffix.lower()
    if ext in IMAGE_EXTS:
        return "image"
    if ext in PDF_EXTS:
        return "pdf"
    return "data"


def should_keep(filename: str, size: int) -> bool:
    ext = pathlib.Path(filename).suffix.lower()
    if ext in IMAGE_EXTS or ext in PDF_EXTS:
        return size <= SIZE_CAP_BYTES
    return size <= SMALL_FILE_CAP_BYTES


def fetch_record_files(record_id: str) -> list[dict] | None:
    """GET the record and return its files[] list, or None on failure."""
    try:
        resp = requests.get(f"{BASE_URL}/{record_id}", headers=HEADERS, timeout=20)
        if resp.status_code == 429:
            print("  Rate limited — sleeping 60s")
            time.sleep(60)
            resp = requests.get(f"{BASE_URL}/{record_id}", headers=HEADERS, timeout=20)
        if resp.status_code == 404:
            return []
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"  [ERROR] record {record_id}: {e}")
        return None

    data = resp.json()
    return data.get("files", []) or []


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
        return total
    except requests.RequestException as e:
        print(f"    [ERROR] download {url}: {e}")
        if dest.exists():
            dest.unlink()
        return None


def process_record(raw_path: pathlib.Path) -> str:
    """Returns one of: 'skipped', 'no_files', 'media_saved', 'error'."""
    try:
        raw = json.loads(raw_path.read_text())
    except Exception as e:
        print(f"  [ERROR] read {raw_path.name}: {e}")
        return "error"

    if "media" in raw:
        return "skipped"

    record_id = raw.get("source_id") or raw_path.stem
    files = fetch_record_files(record_id)
    if files is None:
        return "error"

    manifest = []
    for file_info in files:
        filename = sanitize_filename(file_info.get("key", ""))
        size = int(file_info.get("size", 0) or 0)
        link = (file_info.get("links") or {}).get("self", "")
        if not filename or not link:
            continue
        if not should_keep(filename, size):
            continue

        dest = MEDIA_DIR / record_id / filename
        downloaded_bytes = download_file(link, dest)
        if downloaded_bytes is None:
            continue

        manifest.append({
            "type": media_type_for(filename),
            "filename": filename,
            "local_path": str(dest),
            "source_url": link,
            "caption": "",
            "bytes": downloaded_bytes,
        })

    raw["media"] = manifest
    raw_path.write_text(json.dumps(raw, indent=2, ensure_ascii=False))

    if manifest:
        print(f"  [{record_id}] {len(manifest)} media file(s) saved")
    return "media_saved" if manifest else "no_files"


def main():
    all_records = sorted(RAW_DIR.glob("*.json"))
    print(f"Found {len(all_records)} Zenodo raw records.")

    processed = saved = empty = skipped = errors = 0

    for raw_path in all_records:
        if processed >= MAX_RECORDS:
            break
        result = process_record(raw_path)
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

    print(f"\nDone. Processed: {processed} | With media: {saved} | No media: {empty} | Errors: {errors} | Already checked (skipped): {skipped}")


if __name__ == "__main__":
    main()
