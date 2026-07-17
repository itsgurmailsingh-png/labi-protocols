"""
pull_raw_zenodo.py
Pull full Zenodo API response for every Zenodo record we have.
Saves: data/raw/zenodo/{record_id}.json — untouched API JSON.
"""
import json, re, time, sys
import requests
from pathlib import Path

MERGED  = Path("data/merged")
OUT     = Path("data/raw/zenodo")
BASE    = "https://zenodo.org/api/records"
DELAY   = 0.5

def record_id_from_url(url):
    m = re.search(r'zenodo\.org/records?/(\d+)', url or "")
    return m.group(1) if m else None

def main():
    merged_files = list(MERGED.glob("*.json"))
    records = {}
    for f in merged_files:
        try:
            d = json.loads(f.read_text())
            url = d.get("source_url", "") or ""
            if "zenodo" not in url:
                continue
            rid = record_id_from_url(url)
            if rid:
                records[rid] = f.stem
        except Exception:
            pass

    total = len(records)
    print(f"Zenodo records to fetch: {total}")

    done = skipped = failed = 0
    for i, (rid, stem) in enumerate(records.items(), 1):
        out_file = OUT / f"{rid}.json"
        if out_file.exists():
            skipped += 1
            continue

        try:
            r = requests.get(f"{BASE}/{rid}", timeout=30)
            if r.status_code == 429:
                time.sleep(60)
                r = requests.get(f"{BASE}/{rid}", timeout=30)
            if r.status_code == 200:
                out_file.write_bytes(r.content)
                done += 1
            else:
                print(f"  [{i}/{total}] HTTP {r.status_code} record {rid}")
                failed += 1
        except Exception as e:
            print(f"  [{i}/{total}] ERROR {rid}: {e}")
            failed += 1

        if i % 50 == 0 or i <= 3:
            print(f"  [{i}/{total}] done={done} skip={skipped} fail={failed}")

        time.sleep(DELAY)

    print(f"\nDone. fetched={done} skipped={skipped} failed={failed}")

if __name__ == "__main__":
    main()
