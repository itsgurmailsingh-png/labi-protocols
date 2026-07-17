"""
backfill_protocols_io_steps.py

Fixes a real content-loss bug for protocols.io-sourced protocols: the step
text protocols.io returns lives in either the step-level "section" field or
the step-level "step" (HTML body) field, depending on which editor format
the protocol was authored in — never both. The pipeline's original
extraction only ever tried the "components[].description/body" fields
(wrong nesting — the real text is under components[].source.title/
description), so any protocol using "section" for content came through with
0 real steps. Verified against data/raw/protocols_io/*.json (untouched
cached API responses, 6,101 of 6,291 protocols.io protocols) with mixed
random sampling: some protocols carry real content in "step", others in
"section", never trust one field alone.

This is a fully offline, no-network backfill — it only reconciles our
already-fetched data. Read-only cache (data/raw/protocols_io/) → rewrites
data/sources/protocols_io/raw/{slug}.json's steps_raw ONLY when the cache
has real content we're currently missing or under-representing.

Usage:
    python3 scripts/backfill_protocols_io_steps.py            # apply fixes
    python3 scripts/backfill_protocols_io_steps.py --dry-run  # report only
"""

import argparse
import html
import json
import pathlib
import re

CACHE_DIR = pathlib.Path("data/raw/protocols_io")
RAW_DIR   = pathlib.Path("data/sources/protocols_io/raw")

PLACEHOLDER_RE = re.compile(r"^(<p>\s*</p>\s*)+$")


def strip_html(text: str) -> str:
    text = html.unescape(text or "")
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s{2,}", " ", text)
    return text.strip()


def is_meaningful(text: str) -> bool:
    """True if raw HTML/text has real content (not empty/placeholder)."""
    if not text:
        return False
    if PLACEHOLDER_RE.match(text.strip()):
        return False
    return len(strip_html(text)) >= 3


def extract_component_text(step: dict) -> str:
    """Fallback: walk components[].source.{title,description} for text."""
    parts = []
    for comp in step.get("components") or []:
        src = comp.get("source") or {}
        for field in ("title", "description"):
            val = src.get(field)
            if is_meaningful(val):
                parts.append(strip_html(val))
                break
    return " ".join(parts).strip()


def extract_steps_from_cache(proto: dict) -> list[dict]:
    """
    Returns a list of {"title": str, "instruction": str} dicts using
    whichever field (section / step / components) actually has content
    for each individual step. Steps with no content anywhere are dropped.
    """
    result = []
    for raw_step in proto.get("steps") or []:
        section = raw_step.get("section") or ""
        step_html = raw_step.get("step") or ""

        section_has = is_meaningful(section)
        step_has = is_meaningful(step_html)

        title = ""
        instruction = ""

        if section_has and step_has:
            # Both populated — section is usually a short heading, step the body.
            plain_section = strip_html(section)
            if len(plain_section) <= 80:
                title = plain_section
                instruction = strip_html(step_html)
            else:
                instruction = f"{plain_section} {strip_html(step_html)}".strip()
        elif section_has:
            instruction = strip_html(section)
        elif step_has:
            instruction = strip_html(step_html)
        else:
            instruction = extract_component_text(raw_step)

        if instruction:
            result.append({"title": title, "instruction": instruction})

    return result


def process(dry_run: bool) -> None:
    cache_files = {f.stem: f for f in CACHE_DIR.glob("*.json")}
    raw_files = sorted(RAW_DIR.glob("*.json"))

    checked = improved = unchanged = no_cache = still_empty = 0

    for raw_path in raw_files:
        slug = raw_path.stem
        cache_path = cache_files.get(slug)
        if not cache_path:
            no_cache += 1
            continue

        checked += 1
        try:
            raw = json.loads(raw_path.read_text())
            cached = json.loads(cache_path.read_text())
        except Exception as e:
            print(f"  [ERROR] {slug}: {e}")
            continue

        proto = cached.get("protocol") or cached
        new_steps = extract_steps_from_cache(proto)

        current = raw.get("steps_raw") or []
        current_len = len(current) if isinstance(current, list) else 0

        # Only overwrite when the cache genuinely has more/better content.
        if len(new_steps) <= current_len:
            unchanged += 1
            if not new_steps and not current:
                still_empty += 1
            continue

        improved += 1
        if dry_run:
            print(f"  [WOULD FIX] {slug}: {current_len} -> {len(new_steps)} steps")
        else:
            raw["steps_raw"] = new_steps
            raw_path.write_text(json.dumps(raw, indent=2, ensure_ascii=False))
            print(f"  [FIXED] {slug}: {current_len} -> {len(new_steps)} steps")

    print(f"\nChecked: {checked} | Improved: {improved} | Unchanged: {unchanged} "
          f"| Still empty (no recoverable content): {still_empty} | No cache available: {no_cache}")
    if dry_run:
        print("(dry run — no files written)")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    process(args.dry_run)


if __name__ == "__main__":
    main()
