"""
pull_raw_pmc.py
Pull full PMC XML for every PMC article we have.
Saves: data/raw/pmc/{PMCID}.xml — untouched EFetch XML.
"""
import json, os, re, time, sys
import requests
from pathlib import Path

MERGED   = Path("data/merged")
OUT      = Path("data/raw/pmc")
EFETCH   = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
API_KEY  = os.environ.get("NCBI_API_KEY", "")
DELAY    = 0.11  # ~9 req/s with key (limit is 10/s)

def pmcid_from_url(url):
    m = re.search(r'PMC(\d+)', url or "")
    return f"PMC{m.group(1)}" if m else None

def main():
    pmcids = {}
    # Pull PMCIDs from all source normalised dirs
    sources_dir = Path("data/sources")
    for src in ["star_protocols", "methodsx", "biological_procedures", "current_protocols", "bio_protocol"]:
        norm_dir = sources_dir / src / "normalised"
        if not norm_dir.exists():
            continue
        for f in norm_dir.glob("*.json"):
            try:
                d = json.loads(f.read_text())
                url = d.get("source_url", "") or ""
                pmcid = pmcid_from_url(url)
                if not pmcid:
                    stem = f.stem
                    if stem.startswith("PMC"):
                        pmcid = stem
                    elif stem.isdigit():
                        pmcid = f"PMC{stem}"
                if pmcid:
                    pmcids[pmcid] = f.stem
            except Exception:
                pass
    # Also check merged files
    for f in MERGED.glob("*.json"):
        try:
            d = json.loads(f.read_text())
            url = d.get("source_url", "") or ""
            if "ncbi" in url or "pmc" in url.lower():
                pmcid = pmcid_from_url(url)
                if pmcid:
                    pmcids[pmcid] = f.stem
        except Exception:
            pass

    total = len(pmcids)
    print(f"PMC articles to fetch: {total}")

    params_base = {"db": "pmc", "rettype": "xml"}
    if API_KEY:
        params_base["api_key"] = API_KEY

    done = skipped = failed = 0
    for i, (pmcid, stem) in enumerate(pmcids.items(), 1):
        out_file = OUT / f"{pmcid}.xml"
        if out_file.exists():
            skipped += 1
            continue

        numeric_id = pmcid.replace("PMC", "")
        params = {**params_base, "id": numeric_id}

        for attempt in range(4):
            try:
                r = requests.get(EFETCH, params=params, timeout=45)
                if r.status_code == 429:
                    wait = (attempt + 1) * 15
                    print(f"  [429] sleeping {wait}s")
                    time.sleep(wait)
                    continue
                if r.status_code == 200:
                    out_file.write_bytes(r.content)
                    done += 1
                    break
                else:
                    print(f"  [{i}/{total}] HTTP {r.status_code} {pmcid}")
                    failed += 1
                    break
            except Exception as e:
                print(f"  [{i}/{total}] ERROR {pmcid}: {e}")
                if attempt < 3:
                    time.sleep(5)
                else:
                    failed += 1

        if i % 100 == 0 or i <= 5:
            print(f"  [{i}/{total}] done={done} skip={skipped} fail={failed}  {pmcid}")

        time.sleep(DELAY)

    print(f"\nDone. fetched={done} skipped={skipped} failed={failed}")

if __name__ == "__main__":
    main()
