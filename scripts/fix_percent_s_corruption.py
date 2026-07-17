"""
fix_percent_s_corruption.py

Strips literal "%s" placeholder tokens that leak into published step text.
Root cause: protocols.io's own API serves malformed templated HTML for
table-type step components (primer/oligo tables, reagent tables) —
"<table><thead><tr>%s</tr></thead><tbody>%s</tbody></table>" with the %s
never substituted. The actual table data (sequences, concentrations) was
never delivered by their API at all — this is not recoverable, it's a
protocols.io platform bug, confirmed by checking the untouched cached API
response (data/raw/protocols_io/*.json) which has the same broken markup.

In every sampled case, the %s trails at the end of an otherwise-coherent
instruction ("...according to the table below, and mix briefly %s %s") —
stripping it leaves genuinely useful, complete-reading text. If stripping
would leave a step/material entry empty or trivial, that entry is dropped
entirely rather than left as a stub.

Usage:
    python3 scripts/fix_percent_s_corruption.py --dry-run
    python3 scripts/fix_percent_s_corruption.py
"""

import argparse
import json
import pathlib
import re

SOURCES_DIR = pathlib.Path("data/sources")
_PLACEHOLDER = re.compile(r"(?<![A-Za-z0-9_])%s(?![A-Za-z0-9_])")


def clean_text(text: str) -> str:
    if not text:
        return text
    cleaned = _PLACEHOLDER.sub("", text)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
    return cleaned


def process(dry_run: bool) -> None:
    files_changed = steps_cleaned = steps_dropped = materials_cleaned = materials_dropped = 0

    for norm_path in sorted(SOURCES_DIR.glob("*/normalised/*.json")):
        try:
            d = json.loads(norm_path.read_text())
        except Exception:
            continue

        changed = False

        steps = d.get("steps") or []
        new_steps = []
        for s in steps:
            instr = s.get("instruction") or ""
            title = s.get("title") or ""
            if not _PLACEHOLDER.search(instr) and not _PLACEHOLDER.search(title):
                new_steps.append(s)
                continue

            changed = True
            cleaned_instr = clean_text(instr)
            cleaned_title = clean_text(title)

            if len(cleaned_instr) < 10:
                steps_dropped += 1
                continue

            s["instruction"] = cleaned_instr
            s["title"] = cleaned_title
            steps_cleaned += 1
            new_steps.append(s)

        if changed:
            for i, s in enumerate(new_steps):
                s["step_id"] = i
            d["steps"] = new_steps

        materials = d.get("materials") or []
        new_materials = []
        for m in materials:
            if not _PLACEHOLDER.search(m):
                new_materials.append(m)
                continue
            changed = True
            cleaned = clean_text(m)
            if len(cleaned) < 3:
                materials_dropped += 1
                continue
            new_materials.append(cleaned)
            materials_cleaned += 1

        if materials != new_materials:
            d["materials"] = new_materials
            changed = True

        if changed:
            files_changed += 1
            if not dry_run:
                norm_path.write_text(json.dumps(d, ensure_ascii=False, indent=2))

    print(f"Files changed: {files_changed}")
    print(f"Steps cleaned: {steps_cleaned} | dropped (empty after cleanup): {steps_dropped}")
    print(f"Materials cleaned: {materials_cleaned} | dropped: {materials_dropped}")
    if dry_run:
        print("(dry run — no files written)")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    process(args.dry_run)


if __name__ == "__main__":
    main()
