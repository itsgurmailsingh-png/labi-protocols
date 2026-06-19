#!/usr/bin/env python3
"""
build_index.py — Build index.json for the Labi website CDN.

Reads all normalised files from data/sources/*/normalised/*.json and
produces index.json at the repo root in the format expected by the website:

  {
    "id":            string,
    "title":         string,
    "category":      string,
    "tags":          string[],
    "estimatedTime": string | null,
    "stepCount":     number | null,
    "source_url":    string | null,
    "license":       string | null
  }

Usage:
    cd labi-protocols
    python3 scripts/build_index.py
    python3 scripts/build_index.py --min-quality 0  # include everything
"""

import argparse
import json
import re
import sys
from pathlib import Path

SOURCES_DIR = Path("data/sources")
OUTPUT      = Path("index.json")

# Exclude sources still being processed or empty
SKIP_SOURCES = set()


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return slug[:80] or "protocol"


def format_time(mins) -> str | None:
    if mins is None:
        return None
    try:
        mins = int(mins)
    except (TypeError, ValueError):
        return None
    if mins <= 0:
        return None
    if mins < 60:
        return f"{mins} min"
    h = mins // 60
    m = mins % 60
    return f"{h}h {m}m" if m else f"{h}h"


def load_normalised() -> list[dict]:
    records = []
    seen_titles: set[str] = set()   # simple dedup on normalised title

    for source_dir in sorted(SOURCES_DIR.iterdir()):
        if not source_dir.is_dir():
            continue
        if source_dir.name in SKIP_SOURCES:
            continue
        norm_dir = source_dir / "normalised"
        if not norm_dir.exists():
            continue

        files = sorted(norm_dir.glob("*.json"))
        loaded = 0
        for f in files:
            try:
                data = json.loads(f.read_text())
            except Exception:
                continue

            # Handle list-wrapped records
            items = data if isinstance(data, list) else [data]
            for item in items:
                title = (item.get("title") or "").strip()
                if not title or len(title) < 5:
                    continue

                # Require at least 3 usable steps
                steps = item.get("steps") or item.get("steps_raw") or []
                if len(steps) < 3:
                    continue

                # Simple title dedup
                norm_title = re.sub(r"\s+", " ", title.lower())
                if norm_title in seen_titles:
                    continue
                seen_titles.add(norm_title)

                item.setdefault("source_name", source_dir.name)
                records.append(item)
                loaded += 1

        print(f"  {source_dir.name:25s} {loaded:>5} records")

    return records


def build_entry(item: dict) -> dict:
    title = (item.get("title") or "").strip()

    # id: prefer existing protocol_id, else slugify title
    pid = item.get("protocol_id") or slugify(title)

    # category
    category = (item.get("category") or "Other").strip()

    # tags
    tags = item.get("tags") or []
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",") if t.strip()]
    tags = [str(t).lower().strip() for t in tags if t][:8]

    # estimated time
    estimated_time = format_time(item.get("estimated_time_mins"))

    # step count
    steps = item.get("steps") or item.get("steps_raw") or []
    step_count = len(steps) if steps else None

    # source url
    source_url = item.get("source_url") or item.get("doi") or None

    # license
    license_val = item.get("license") or None

    return {
        "id":            pid,
        "title":         title,
        "category":      category,
        "tags":          tags,
        "estimatedTime": estimated_time,
        "stepCount":     step_count,
        "source_url":    source_url,
        "license":       license_val,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=str(OUTPUT), help="Output path")
    args = parser.parse_args()

    print("=== build_index.py ===")
    print(f"Loading normalised records from {SOURCES_DIR}/")
    records = load_normalised()
    print(f"\nTotal loaded: {len(records)}")

    entries = [build_entry(r) for r in records]

    # Sort: category asc, title asc
    entries.sort(key=lambda e: (e["category"], e["title"].lower()))

    out_path = Path(args.out)
    out_path.write_text(json.dumps(entries, ensure_ascii=False, separators=(",", ":")))

    # Category breakdown
    cats: dict[str, int] = {}
    for e in entries:
        cats[e["category"]] = cats.get(e["category"], 0) + 1

    print("\nCategory breakdown:")
    for cat, count in sorted(cats.items(), key=lambda x: -x[1]):
        print(f"  {cat:40s} {count:>5}")

    print(f"\n✅ Wrote {len(entries)} entries to {out_path} ({out_path.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
