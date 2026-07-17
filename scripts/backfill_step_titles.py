"""
backfill_step_titles.py

Adds a short scannable title to every step that's missing one. Purely
additive — never touches instruction text, so there's no content-loss risk
(unlike split_inline_numbered, this can't scramble anything since it only
sets a field that was empty).

Reuses fix_broken_steps.py::infer_title(), the same heuristic already
proven safe there (title inference on already-known-good steps).

Usage:
    python3 scripts/backfill_step_titles.py --dry-run
    python3 scripts/backfill_step_titles.py
"""

import argparse
import importlib.util
import json
import pathlib

SOURCES_DIR = pathlib.Path("data/sources")

spec = importlib.util.spec_from_file_location("fix_broken_steps", pathlib.Path("scripts/fix_broken_steps.py"))
fbs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fbs)


def process(dry_run: bool) -> None:
    files_changed = steps_titled = 0

    for norm_path in sorted(SOURCES_DIR.glob("*/normalised/*.json")):
        try:
            d = json.loads(norm_path.read_text())
        except Exception:
            continue

        steps = d.get("steps") or []
        if not steps:
            continue

        changed = False
        for s in steps:
            if not (s.get("title") or "").strip():
                instr = s.get("instruction") or ""
                if instr.strip():
                    s["title"] = fbs.infer_title(instr)
                    changed = True
                    steps_titled += 1

        if changed:
            files_changed += 1
            if not dry_run:
                norm_path.write_text(json.dumps(d, ensure_ascii=False, indent=2))

    print(f"Files changed: {files_changed}")
    print(f"Steps titled:  {steps_titled}")
    if dry_run:
        print("(dry run — no files written)")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    process(args.dry_run)


if __name__ == "__main__":
    main()
