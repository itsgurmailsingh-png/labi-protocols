"""
Fetch figure images for PMC-family protocols flagged as referencing a
figure with no media attached (logs/missing_media_flagged.jsonl).

Path: NCBI's OA service (oa.fcgi) returns a package href, but as of 2026
its FTP tree was reorganised — oa_package moved under deprecated/ (still
live, scheduled for removal ~August 2026). We rewrite the returned href to
the current https deprecated/ path and download the tarball there.

Writes downloaded images to data/media/{source}/{pmc_id}/ and updates the
`media` field in data/sources/{source}/normalised/{pmc_id}.json.

Usage:
    python3 scripts/fetch_pmc_figures.py
    python3 scripts/fetch_pmc_figures.py --limit 20   # test on a subset
"""

import argparse
import json
import pathlib
import re
import shutil
import tarfile
import time
import urllib.request

SOURCES_DIR = pathlib.Path("data/sources")
MEDIA_DIR = pathlib.Path("data/media")
FLAGGED = pathlib.Path("logs/missing_media_flagged.jsonl")

OA_ENDPOINT = "https://www.ncbi.nlm.nih.gov/pmc/utils/oa/oa.fcgi?id={pmc_id}"
SIZE_CAP_BYTES = 8 * 1024 * 1024
REQUEST_TIMEOUT = 30
MIN_FREE_BYTES = 1_500_000_000  # abort if free disk drops below this — disk crashed once already this session
HEADERS = {"User-Agent": "Mozilla/5.0 (labi-protocols research pipeline)"}


def deprecated_https_url(ftp_href: str) -> str:
    # ftp://ftp.ncbi.nlm.nih.gov/pub/pmc/oa_package/79/ba/PMC123.tar.gz
    # -> https://ftp.ncbi.nlm.nih.gov/pub/pmc/deprecated/oa_package/79/ba/PMC123.tar.gz
    path = ftp_href.split("ftp.ncbi.nlm.nih.gov", 1)[-1]
    path = path.replace("/pub/pmc/oa_package/", "/pub/pmc/deprecated/oa_package/")
    return f"https://ftp.ncbi.nlm.nih.gov{path}"


def get_package_url(pmc_id: str) -> str | None:
    req = urllib.request.Request(OA_ENDPOINT.format(pmc_id=pmc_id), headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            body = resp.read().decode("utf-8", errors="ignore")
    except Exception as e:
        print(f"    [OA lookup error] {pmc_id}: {e}")
        return None
    m = re.search(r'href="([^"]+\.tar\.gz)"', body)
    if not m:
        return None
    return deprecated_https_url(m.group(1))


def fetch_and_extract(pmc_id: str, dest_dir: pathlib.Path) -> list[dict]:
    url = get_package_url(pmc_id)
    if not url:
        return []

    tmp_tar = dest_dir.parent / f"_tmp_{pmc_id}.tar.gz"
    dest_dir.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            data = resp.read()
    except Exception as e:
        print(f"    [download error] {pmc_id}: {e}")
        return []

    tmp_tar.write_bytes(data)
    entries = []
    try:
        with tarfile.open(tmp_tar) as tf:
            for member in tf.getmembers():
                name = pathlib.Path(member.name).name
                if not name.lower().endswith((".jpg", ".jpeg", ".png")):
                    continue
                if name.lower().startswith("ga"):  # skip graphical-abstract-only
                    continue
                if member.size > SIZE_CAP_BYTES:
                    continue
                f = tf.extractfile(member)
                if not f:
                    continue
                out_path = dest_dir / name
                out_path.write_bytes(f.read())
                entries.append({
                    "type": "image",
                    "filename": name,
                    "local_path": str(out_path),
                    "source_url": f"https://www.ncbi.nlm.nih.gov/pmc/articles/{pmc_id}/",
                    "caption": "",
                    "bytes": out_path.stat().st_size,
                })
    except tarfile.TarError as e:
        print(f"    [extract error] {pmc_id}: {e}")
    finally:
        tmp_tar.unlink(missing_ok=True)

    return entries


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()

    flagged = [json.loads(l) for l in FLAGGED.open()]

    # pubmed_central had zero hits in the text-pattern check (missed entirely —
    # confirmed live via efetch that it has real <graphic> tags just like its
    # PMC siblings), so add all of it directly rather than trust that check.
    for f in (SOURCES_DIR / "pubmed_central" / "normalised").glob("*.json"):
        try:
            d = json.loads(f.read_text())
        except Exception:
            continue
        if d.get("protocol_id") and not d.get("media"):
            flagged.append({"protocol_id": d["protocol_id"], "source": "pubmed_central"})

    lookup = {}
    for src in {x["source"] for x in flagged}:
        for f in (SOURCES_DIR / src / "normalised").glob("*.json"):
            try:
                d = json.loads(f.read_text())
            except Exception:
                continue
            pid = d.get("protocol_id")
            if pid:
                lookup[(src, pid)] = f

    fetched = 0
    no_package = 0
    already_linked = 0
    processed = 0

    for item in flagged:
        if args.limit and processed >= args.limit:
            break
        if shutil.disk_usage("/").free < MIN_FREE_BYTES:
            print(f"\n[ABORT] Free disk below {MIN_FREE_BYTES/1e9:.1f}GB safety floor. Stopping.")
            break
        pid, src = item["protocol_id"], item["source"]
        norm_path = lookup.get((src, pid))
        if not norm_path:
            continue
        stem = norm_path.stem
        if not stem.startswith("PMC"):
            continue  # not a PMC-family record, skip (protocols_io etc. need a different path)

        d = json.loads(norm_path.read_text())
        if d.get("media"):
            already_linked += 1
            continue

        processed += 1
        media_dir = MEDIA_DIR / src / stem
        entries = fetch_and_extract(stem, media_dir)
        if entries:
            d["media"] = entries
            norm_path.write_text(json.dumps(d, ensure_ascii=False, indent=2))
            fetched += 1
            print(f"  [{src}] {stem}: {len(entries)} images")
        else:
            no_package += 1
        time.sleep(0.3)

    print(f"\nFetched media for {fetched} protocols.")
    print(f"No OA package / no images found for {no_package}.")
    print(f"Already had media (skipped): {already_linked}")


if __name__ == "__main__":
    main()
