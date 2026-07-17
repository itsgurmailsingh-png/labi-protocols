"""
Link already-downloaded PMC figure images into their protocol's `media`
field. These files exist on disk (data/media/{source}/{pmc_id}/*.jpg) from
an earlier fetch pass but were never written into the normalised JSON, so
they've been sitting unused.

Writes to data/sources/{source}/normalised/{pmc_id}.json — the canonical
source that merge_and_dedup.py rebuilds protocols/ from.

Usage:
    python3 scripts/link_existing_pmc_media.py
"""

import json
import pathlib

SOURCES_DIR = pathlib.Path("data/sources")
MEDIA_DIR = pathlib.Path("data/media")
FLAGGED = pathlib.Path("logs/missing_media_flagged.jsonl")

# figures over this size aren't worth bloating the repo for a thumbnail-scale image
SIZE_CAP_BYTES = 8 * 1024 * 1024


def main():
    flagged = [json.loads(l) for l in FLAGGED.open()]

    # protocol_id -> normalised file path, per source
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

    linked = 0
    skipped_no_disk = 0
    for item in flagged:
        pid, src = item["protocol_id"], item["source"]
        norm_path = lookup.get((src, pid))
        if not norm_path:
            skipped_no_disk += 1
            continue

        stem = norm_path.stem
        media_dir = MEDIA_DIR / src / stem
        images = sorted(media_dir.glob("*.jpg")) if media_dir.exists() else []
        # prefer real figures (gr*/fx*) over graphical-abstract-only (ga*)
        figures = [p for p in images if not p.stem.startswith("ga")] or images
        if not figures:
            skipped_no_disk += 1
            continue

        d = json.loads(norm_path.read_text())
        if d.get("media"):
            continue  # already has media, don't duplicate

        existing_names = set()
        media_entries = []
        for img in figures:
            size = img.stat().st_size
            if size > SIZE_CAP_BYTES or img.name in existing_names:
                continue
            existing_names.add(img.name)
            media_entries.append({
                "type": "image",
                "filename": img.name,
                "local_path": str(img),
                "source_url": d.get("source_url", ""),
                "caption": "",
                "bytes": size,
            })

        if media_entries:
            d["media"] = media_entries
            norm_path.write_text(json.dumps(d, ensure_ascii=False, indent=2))
            linked += 1

    print(f"Linked existing media into {linked} protocols.")
    print(f"No usable local files found for {skipped_no_disk} (need real fetch).")


if __name__ == "__main__":
    main()
