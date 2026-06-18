"""
Backfill sub-step detection on already-normalised protocol files.
Reads each normalised JSON, runs _extract_substeps on each step's instruction,
and writes the updated file back in place. No LLM calls.

Usage:
    python scripts/add_substeps.py
    python scripts/add_substeps.py --source protocols_io
"""

import json
import re
import pathlib
import argparse

SOURCES_DIR = pathlib.Path("data/sources")
KNOWN_SOURCES = [
    "bio_protocol", "pubmed_central", "vendor",
    "protocols_io", "zenodo", "figshare",
    "openwetware", "github_opentrons",
]


def _extract_substeps(instruction: str) -> list | None:
    # Lettered sub-steps: a) b) c) (newline or inline)
    alpha_split = re.split(r"(?:^|\n)\s*[a-z]\)\s+", instruction, flags=re.MULTILINE)
    if len(alpha_split) >= 4:
        subs = [s.strip() for s in alpha_split if s.strip() and len(s.strip()) > 10]
        if len(subs) >= 3:
            return subs

    inline_alpha = re.split(r"\s+[a-z]\)\s+", instruction)
    if len(inline_alpha) >= 3:
        subs = [s.strip() for s in inline_alpha if s.strip() and len(s.strip()) > 10]
        if len(subs) >= 3:
            return subs

    # Sub-numbered: 1a. 1b. or 1.1 1.2
    subnumbered = re.split(r"(?:^|\n)\s*\d+[a-z][\.\)]\s+|\s*\d+\.\d+\.?\s+", instruction, flags=re.MULTILINE)
    if len(subnumbered) >= 4:
        subs = [s.strip() for s in subnumbered if s.strip() and len(s.strip()) > 10]
        if len(subs) >= 3:
            return subs

    # Bullet lines
    lines = instruction.split("\n")
    bullet_lines = [re.sub(r"^[\s•\-–\*]+", "", l).strip() for l in lines if re.match(r"^\s*[•\-–\*]\s+", l)]
    if len(bullet_lines) >= 3:
        return [b for b in bullet_lines if len(b) > 10]

    # Roman numerals
    roman_split = re.split(r"(?:^|\n)\s*(?:i{1,3}|iv|vi{0,3}|ix|x)[\.\)]\s+", instruction, flags=re.MULTILINE | re.IGNORECASE)
    if len(roman_split) >= 4:
        subs = [s.strip() for s in roman_split if s.strip() and len(s.strip()) > 10]
        if len(subs) >= 3:
            return subs

    return None


def backfill_source(source_name: str) -> tuple[int, int]:
    norm_dir = SOURCES_DIR / source_name / "normalised"
    if not norm_dir.exists():
        return 0, 0

    files = sorted(norm_dir.glob("*.json"))
    updated = unchanged = 0

    for path in files:
        try:
            data = json.loads(path.read_text())
        except Exception:
            continue

        steps = data.get("steps", [])
        changed = False

        for step in steps:
            if not isinstance(step, dict):
                continue
            # Add substeps field if missing
            if "substeps" not in step:
                instruction = step.get("instruction", "")
                raw_subs = _extract_substeps(instruction) if instruction else None
                step["substeps"] = [{"instruction": s, "title": ""} for s in raw_subs] if raw_subs else []
                changed = True

        if changed:
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2))
            updated += 1
        else:
            unchanged += 1

    return updated, unchanged


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", choices=KNOWN_SOURCES)
    args = parser.parse_args()

    sources = [args.source] if args.source else KNOWN_SOURCES
    total_updated = total_unchanged = 0

    for source in sources:
        print(f"── {source} ...", end=" ", flush=True)
        u, s = backfill_source(source)
        print(f"{u} updated, {s} unchanged")
        total_updated += u
        total_unchanged += s

    print(f"\nDone: {total_updated} files updated, {total_unchanged} unchanged")


if __name__ == "__main__":
    main()
