"""
Fetch protocols.io's per-protocol cover image (`protocol_image_file.url`)
AND supplementary documents (`documents[]` — PDF/DOCX/XLSX attachments),
neither of which the original fetcher ever captured.

Confirmed live via sampling: ~52% of protocols.io protocols have a real
(non-default) uploaded cover image, and ~10% have real supplementary
documents (including genuine .xlsx files) — both sitting on protocols.io's
own CDN, completely uncaptured. One API call per protocol gets both.

Writes to data/media/protocols_io/{slug}/{filename} and updates the
`media` field in data/sources/protocols_io/normalised/{slug}.json.

Resume-safe: skips any normalised file that already has `media` set, or
that's already been checked and confirmed to have nothing (marked via
`_cover_image_checked`).

Usage:
    python3 scripts/fetch_protocols_io_cover_images.py
    python3 scripts/fetch_protocols_io_cover_images.py --limit 20
"""

import argparse
import json
import os
import pathlib
import shutil
import time
import urllib.request

MIN_FREE_BYTES = 1_500_000_000  # abort if free disk drops below this — disk crashed once already this session

NORM_DIR = pathlib.Path("data/sources/protocols_io/normalised")
MEDIA_DIR = pathlib.Path("data/media/protocols_io")

_env_path = pathlib.Path(__file__).resolve().parent.parent / ".env"
if _env_path.exists():
    for line in _env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

TOKEN = os.environ.get("PROTOCOLS_IO_TOKEN", "")
API_BASE = "https://www.protocols.io/api/v3/protocols"
REQUEST_TIMEOUT = 20
SIZE_CAP_BYTES = 8 * 1024 * 1024
DOC_EXTENSIONS = {"pdf", "docx", "doc", "xlsx", "xls", "csv"}


def slug_from_url(source_url: str) -> str:
    return source_url.rstrip("/").split("/")[-1]


class ApiError(Exception):
    pass


def fetch_protocol_assets(slug: str) -> tuple[str | None, list[dict]]:
    """Returns (cover_image_url_or_None, list_of_document_dicts).
    Raises ApiError on request failure — callers must NOT mark the file
    checked on this path, only on a genuine (possibly empty) result."""
    req = urllib.request.Request(
        f"{API_BASE}/{slug}",
        headers={"Authorization": f"Bearer {TOKEN}", "User-Agent": "Mozilla/5.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            data = json.loads(resp.read())
    except Exception as e:
        raise ApiError(str(e))

    proto = data.get("protocol") or {}
    img_url = (proto.get("protocol_image_file") or {}).get("url", "")
    if not img_url or "default_protocol" in img_url:
        img_url = None

    docs = []
    for doc in proto.get("documents") or []:
        ofn = doc.get("ofn", "")
        ext = ofn.split(".")[-1].lower() if "." in ofn else ""
        url = doc.get("url", "")
        size = doc.get("size", 0)
        if ext in DOC_EXTENSIONS and url and size and size < SIZE_CAP_BYTES:
            docs.append({"url": url, "filename": ofn, "ext": ext, "size": size})

    return img_url, docs


def download_file(url: str, dest_dir: pathlib.Path, media_type: str, filename: str | None = None) -> dict | None:
    filename = filename or (url.rstrip("/").split("/")[-1] or "file")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            data = resp.read()
    except Exception as e:
        print(f"    [download error] {url}: {e}")
        return None
    if len(data) > SIZE_CAP_BYTES:
        return None
    dest_dir.mkdir(parents=True, exist_ok=True)
    out_path = dest_dir / filename
    out_path.write_bytes(data)
    return {
        "type": media_type,
        "filename": filename,
        "local_path": str(out_path),
        "source_url": url,
        "caption": "",
        "bytes": len(data),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()

    if not TOKEN:
        print("PROTOCOLS_IO_TOKEN not set, aborting.")
        return

    files = sorted(NORM_DIR.glob("*.json"))
    fetched_img = fetched_docs = no_assets = errors = processed = 0

    for f in files:
        if args.limit and processed >= args.limit:
            break
        if shutil.disk_usage("/").free < MIN_FREE_BYTES:
            print(f"\n[ABORT] Free disk below {MIN_FREE_BYTES/1e9:.1f}GB safety floor. Stopping.")
            break
        d = json.loads(f.read_text())
        if d.get("media") or d.get("_cover_image_checked"):
            continue

        slug = slug_from_url(d.get("source_url", ""))
        if not slug:
            continue

        processed += 1
        try:
            img_url, docs = fetch_protocol_assets(slug)
        except ApiError as e:
            print(f"    [API error] {slug}: {e}")
            errors += 1
            time.sleep(0.25)
            continue  # do NOT mark checked — retry on next run

        entries = []
        if img_url:
            entry = download_file(img_url, MEDIA_DIR / slug, "image")
            if entry:
                entries.append(entry)
                fetched_img += 1
        for doc in docs:
            entry = download_file(doc["url"], MEDIA_DIR / slug, "document", doc["filename"])
            if entry:
                entries.append(entry)
                fetched_docs += 1

        d["_cover_image_checked"] = True
        if entries:
            d["media"] = entries
        else:
            no_assets += 1
        f.write_text(json.dumps(d, ensure_ascii=False, indent=2))

        if (fetched_img + fetched_docs) and (fetched_img + fetched_docs) % 20 == 0:
            print(f"  images={fetched_img} docs={fetched_docs} processed={processed}")

        time.sleep(0.25)

    print(f"\nDone. Real cover images fetched: {fetched_img}")
    print(f"Real documents fetched: {fetched_docs}")
    print(f"Confirmed no assets: {no_assets}")
    print(f"API errors (will retry next run): {errors}")
    print(f"Total processed this run: {processed}")


if __name__ == "__main__":
    main()
