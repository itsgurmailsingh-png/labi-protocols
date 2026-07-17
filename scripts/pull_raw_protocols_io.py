"""
pull_raw_protocols_io.py
Pull the full API response for every protocols.io protocol we have.
Saves: data/raw/protocols_io/{slug}.json — untouched API JSON.
"""
import json, os, re, time, sys
import requests
from pathlib import Path

TOKEN   = os.environ.get("PROTOCOLS_IO_TOKEN", "")
MERGED  = Path("data/merged")
OUT     = Path("data/raw/protocols_io")
BASE    = "https://www.protocols.io/api/v3/protocols"
DELAY   = 0.6   # seconds between requests

HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "User-Agent": "LabiApp/1.0 (mailto:support@labi.app)",
}

def slug_from_url(url):
    url = (url or "").rstrip("/")
    m = re.search(r'protocols\.io/(?:view|private)/([^/?#]+)', url)
    return m.group(1) if m else None

def main():
    if not TOKEN:
        print("[FATAL] PROTOCOLS_IO_TOKEN not set"); sys.exit(1)

    # Collect all slugs from merged files
    merged_files = list(MERGED.glob("*.json"))
    slugs = {}
    for f in merged_files:
        try:
            d = json.loads(f.read_text())
            url = d.get("source_url", "") or ""
            if "protocols.io" not in url:
                continue
            slug = slug_from_url(url)
            if slug:
                slugs[slug] = f.stem
        except Exception:
            pass

    total = len(slugs)
    print(f"protocols.io slugs to fetch: {total}")

    done = skipped = failed = 0
    for i, (slug, stem) in enumerate(slugs.items(), 1):
        out_file = OUT / f"{slug}.json"
        if out_file.exists():
            skipped += 1
            continue

        try:
            r = requests.get(f"{BASE}/{slug}", headers=HEADERS, timeout=30)
            if r.status_code == 429:
                wait = 30
                print(f"  [429] rate limit — sleeping {wait}s")
                time.sleep(wait)
                r = requests.get(f"{BASE}/{slug}", headers=HEADERS, timeout=30)
            if r.status_code == 401:
                print("[FATAL] 401 — token invalid"); sys.exit(1)
            if r.status_code not in (200, 404):
                print(f"  [{i}/{total}] HTTP {r.status_code} {slug}")
                failed += 1
                time.sleep(DELAY)
                continue
            out_file.write_bytes(r.content)
            done += 1
            if i % 100 == 0 or i <= 5:
                print(f"  [{i}/{total}] done={done} skip={skipped} fail={failed}  {slug[:50]}")
        except Exception as e:
            print(f"  [{i}/{total}] ERROR {slug}: {e}")
            failed += 1

        time.sleep(DELAY)

    print(f"\nDone. fetched={done} skipped={skipped} failed={failed}")

if __name__ == "__main__":
    main()
