"""
Fetch real supplementary files (PDF/image/Excel/etc.) for zenodo and
figshare protocols that currently have no media. Both platforms share
the same API shape: a `files` array with name/size/download URL.

Confirmed live via sampling: ~62% of zenodo records and a meaningful
fraction of figshare records have real small-to-medium files (protocol
handbooks, data spreadsheets, figures) that were never fetched.

Writes to data/media/{source}/{record_id}/{filename} and updates the
`media` field in data/sources/{source}/normalised/{record_id}.json.

Usage:
    python3 scripts/fetch_zenodo_figshare_files.py --source zenodo
    python3 scripts/fetch_zenodo_figshare_files.py --source figshare
    python3 scripts/fetch_zenodo_figshare_files.py --source zenodo --limit 20
"""

import argparse
import json
import pathlib
import random
import shutil
import time
import urllib.error
import urllib.request


def request_with_retry(req, timeout, max_attempts=5):
    """Zenodo/figshare rate-limit hard under this workload's request volume
    (confirmed live: bursts of 429s once several downloads happen for one
    record in quick succession). Retry with backoff instead of silently
    dropping the file."""
    for attempt in range(max_attempts):
        try:
            return urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < max_attempts - 1:
                delay = (2 ** attempt) + random.uniform(0, 1)
                time.sleep(delay)
                continue
            raise

SOURCES_DIR = pathlib.Path("data/sources")
MEDIA_DIR = pathlib.Path("data/media")
REQUEST_TIMEOUT = 25
SIZE_CAP_BYTES = 8 * 1024 * 1024
MIN_FREE_BYTES = 1_500_000_000
KEEP_EXTENSIONS = {
    "jpg": "image", "jpeg": "image", "png": "image", "gif": "image", "tif": "image", "tiff": "image",
    "pdf": "document", "docx": "document", "doc": "document",
    "xlsx": "document", "xls": "document", "csv": "document", "pptx": "document",
}


def list_zenodo_files(record_id: str) -> list[dict]:
    # No custom User-Agent — Zenodo's WAF blocks generic "Mozilla/5.0" as an
    # obvious bot signature (confirmed live: curl -A "Mozilla/5.0" -> 403,
    # curl/urllib with their own default UA -> 200). Opposite of protocols.io.
    req = urllib.request.Request(f"https://zenodo.org/api/records/{record_id}")
    with request_with_retry(req, REQUEST_TIMEOUT) as resp:
        data = json.loads(resp.read())
    out = []
    for f in data.get("files") or []:
        name = f.get("key", "")
        size = f.get("size", 0)
        url = (f.get("links") or {}).get("self", "")
        out.append({"name": name, "size": size, "url": url})
    return out


def list_figshare_files(record_id: str) -> list[dict]:
    req = urllib.request.Request(f"https://api.figshare.com/v2/articles/{record_id}")
    with request_with_retry(req, REQUEST_TIMEOUT) as resp:
        data = json.loads(resp.read())
    out = []
    for f in data.get("files") or []:
        out.append({"name": f.get("name", ""), "size": f.get("size", 0), "url": f.get("download_url", "")})
    return out


def download(url: str, dest_dir: pathlib.Path, filename: str, media_type: str) -> dict | None:
    req = urllib.request.Request(url)
    try:
        with request_with_retry(req, REQUEST_TIMEOUT) as resp:
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
    ap.add_argument("--source", required=True, choices=["zenodo", "figshare"])
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()

    lister = list_zenodo_files if args.source == "zenodo" else list_figshare_files
    norm_dir = SOURCES_DIR / args.source / "normalised"

    fetched = no_files = api_errors = processed = 0

    for norm_path in sorted(norm_dir.glob("*.json")):
        if args.limit and processed >= args.limit:
            break
        if shutil.disk_usage("/").free < MIN_FREE_BYTES:
            print(f"\n[ABORT] Free disk below {MIN_FREE_BYTES/1e9:.1f}GB safety floor. Stopping.")
            break

        d = json.loads(norm_path.read_text())
        if d.get("media"):
            continue
        record_id = norm_path.stem

        processed += 1
        try:
            remote_files = lister(record_id)
        except Exception as e:
            print(f"    [API error] {record_id}: {e}")
            api_errors += 1
            time.sleep(0.3)
            continue

        entries = []
        for rf in remote_files:
            ext = rf["name"].split(".")[-1].lower() if "." in rf["name"] else ""
            media_type = KEEP_EXTENSIONS.get(ext)
            if not media_type or not rf["url"] or rf["size"] > SIZE_CAP_BYTES:
                continue
            entry = download(rf["url"], MEDIA_DIR / args.source / record_id, rf["name"], media_type)
            time.sleep(0.2)
            if entry:
                entries.append(entry)

        if entries:
            d["media"] = entries
            norm_path.write_text(json.dumps(d, ensure_ascii=False, indent=2))
            fetched += 1
            if fetched % 20 == 0:
                print(f"  [{args.source}] fetched={fetched} processed={processed}")
        else:
            no_files += 1

        time.sleep(0.3)

    print(f"\nDone. Real media fetched: {fetched}")
    print(f"No usable files: {no_files}")
    print(f"API errors: {api_errors}")
    print(f"Total processed: {processed}")


if __name__ == "__main__":
    main()
