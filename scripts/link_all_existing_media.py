"""
General sweep: link any already-downloaded media sitting on disk
(data/media/{source}/{record_id}/*) into its protocol's `media` field,
across ALL sources — not just the narrow set previously flagged by the
text-pattern check. Found via a disk audit: ~3.5GB of legitimate,
already-fetched files (PDFs, DOCX, images) were sitting unlinked from
earlier work this session.

Zero network calls — this only wires up what's already on disk. Skips
any junk file types (see clean_junk_media.py's KEEP_EXTENSIONS) and any
protocol that already has a media field.

Usage:
    python3 scripts/link_all_existing_media.py
    python3 scripts/link_all_existing_media.py --dry-run
"""

import argparse
import json
import pathlib

SOURCES_DIR = pathlib.Path("data/sources")
MEDIA_DIR = pathlib.Path("data/media")

KEEP_EXTENSIONS = {
    "jpg": "image", "jpeg": "image", "png": "image", "gif": "image",
    "tif": "image", "tiff": "image",
    "pdf": "document", "docx": "document", "doc": "document",
    "xlsx": "document", "xls": "document", "csv": "document", "pptx": "document",
}
SIZE_CAP_BYTES = 8 * 1024 * 1024


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    linked = 0
    bytes_linked = 0

    for source_media_dir in MEDIA_DIR.iterdir():
        if not source_media_dir.is_dir():
            continue
        source = source_media_dir.name
        norm_dir = SOURCES_DIR / source / "normalised"
        if not norm_dir.exists():
            continue

        for record_dir in source_media_dir.iterdir():
            if not record_dir.is_dir():
                continue
            norm_path = norm_dir / f"{record_dir.name}.json"
            if not norm_path.exists():
                continue

            try:
                d = json.loads(norm_path.read_text())
            except Exception:
                continue
            if d.get("media"):
                continue  # already has media, don't touch

            entries = []
            for f in sorted(record_dir.iterdir()):
                if not f.is_file():
                    continue
                ext = f.suffix.lstrip(".").lower()
                media_type = KEEP_EXTENSIONS.get(ext)
                if not media_type:
                    continue
                size = f.stat().st_size
                if size > SIZE_CAP_BYTES:
                    continue
                entries.append({
                    "type": media_type,
                    "filename": f.name,
                    "local_path": str(f),
                    "source_url": d.get("source_url", ""),
                    "caption": "",
                    "bytes": size,
                })

            if entries:
                linked += 1
                bytes_linked += sum(e["bytes"] for e in entries)
                if not args.dry_run:
                    d["media"] = entries
                    norm_path.write_text(json.dumps(d, ensure_ascii=False, indent=2))

    print(f"{'[DRY RUN] Would link' if args.dry_run else 'Linked'} media for {linked} protocols "
          f"({bytes_linked / 1e6:.1f} MB of already-downloaded content put to use).")


if __name__ == "__main__":
    main()
