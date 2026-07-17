"""
Add a real, fetchable `url` field to every media entry, built from its
local_path relative to data/media/ and the labi-protocols-media repo now
hosting those files on GitHub, served via jsDelivr.

Without this, every media entry only has `local_path` — a path on this
machine, not something any app can fetch. `local_path` is kept for our
own internal tooling; `url` is what the app actually needs.

Only rewrites sources confirmed already pushed to labi-protocols-media
(pass via --sources) — running it against a source not yet pushed would
produce URLs that 404.

Usage:
    python3 scripts/add_media_cdn_urls.py --sources methodsx star_protocols protocols_io protocols_io_docs biological_procedures current_protocols pubmed_central
"""

import argparse
import json
import pathlib

SOURCES_DIR = pathlib.Path("data/sources")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sources", nargs="+", required=True)
    ap.add_argument("--branch", default="main")
    args = ap.parse_args()

    cdn_base = f"https://cdn.jsdelivr.net/gh/itsgurmailsingh-png/labi-protocols-media@{args.branch}"
    updated_files = updated_entries = 0

    for source in args.sources:
        for norm_path in (SOURCES_DIR / source / "normalised").glob("*.json"):
            try:
                d = json.loads(norm_path.read_text())
            except Exception:
                continue
            media = d.get("media") or []
            if not media:
                continue

            dirty = False
            for m in media:
                if m.get("url"):
                    continue
                lp = m.get("local_path", "")
                if not lp.startswith("data/media/"):
                    continue
                rel = lp[len("data/media/"):]
                m["url"] = f"{cdn_base}/{rel}"
                dirty = True

            if dirty:
                updated_entries += sum(1 for m in media if m.get("url"))
                norm_path.write_text(json.dumps(d, ensure_ascii=False, indent=2))
                updated_files += 1

    print(f"Added CDN urls to {updated_entries} media entries across {updated_files} protocols.")


if __name__ == "__main__":
    main()
