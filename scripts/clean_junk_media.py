"""
Remove media that isn't actually relevant to running a protocol — videos,
archives, executables, statistical-software data files — pulled in
indiscriminately by an earlier text-extraction pass that grabbed any
attached file regardless of type. Real example found: an Android .apk
attached as "media" to a memory-test protocol.

Keeps: images (jpg/jpeg/png/gif/tif/tiff) and documents someone could
actually read for protocol content (pdf/docx/doc/xlsx/xls/csv/pptx).
Drops everything else, deletes the physical file, and removes the disk
directory if it's now empty.

Usage:
    python3 scripts/clean_junk_media.py
    python3 scripts/clean_junk_media.py --dry-run
"""

import argparse
import json
import pathlib

SOURCES_DIR = pathlib.Path("data/sources")

KEEP_EXTENSIONS = {
    "jpg", "jpeg", "png", "gif", "tif", "tiff",
    "pdf", "docx", "doc", "xlsx", "xls", "csv", "pptx",
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    bytes_freed = 0
    entries_removed = 0
    files_touched = 0

    for norm_path in SOURCES_DIR.glob("*/normalised/*.json"):
        try:
            d = json.loads(norm_path.read_text())
        except Exception:
            continue
        media = d.get("media") or []
        if not media:
            continue

        kept, dropped = [], []
        for m in media:
            ext = m.get("filename", "").split(".")[-1].lower()
            (kept if ext in KEEP_EXTENSIONS else dropped).append(m)

        if not dropped:
            continue

        files_touched += 1
        for m in dropped:
            entries_removed += 1
            bytes_freed += m.get("bytes", 0)
            local_path = m.get("local_path")
            if local_path and not args.dry_run:
                p = pathlib.Path(local_path)
                if p.exists():
                    p.unlink()
                    # remove the per-protocol dir if now empty
                    try:
                        if p.parent.exists() and not any(p.parent.iterdir()):
                            p.parent.rmdir()
                    except OSError:
                        pass

        if not args.dry_run:
            d["media"] = kept
            norm_path.write_text(json.dumps(d, ensure_ascii=False, indent=2))

    print(f"{'[DRY RUN] Would remove' if args.dry_run else 'Removed'} {entries_removed} junk media entries "
          f"across {files_touched} protocols.")
    print(f"{'Would free' if args.dry_run else 'Freed'}: {bytes_freed / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
