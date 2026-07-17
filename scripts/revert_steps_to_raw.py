"""
revert_steps_to_raw.py

EMERGENCY REVERT. fix_broken_steps.py's split_inline_numbered() regex
(`(\\d+)[\\.:\\)]\\s+(?=[A-Z\\w])`) matches inline enumeration like
"...tools (eg, PROBAST) [25]; and (6) narrative synthesis..." — not just
real step boundaries — because its lookahead accepts any word character,
not just a sentence-starting capital. Confirmed on real published data:
sentences get sliced apart at "(1)", "(2)", "(6)" etc. mid-sentence, the
marker itself is discarded, and the fragments end up as separate,
scrambled, truncated top-level steps. This affects the ORIGINAL
(pre-session) fix_broken_steps.py runs AND this session's broad re-run
(11,266 files touched with the same unfixed regex).

This script regenerates the "steps" field of every normalised file
straight from its own raw file's steps_raw, using normalise.py's
steps_from_raw() (which does NOT do risky inline mid-paragraph splitting —
only safe, nested sub-step detection). This undoes ALL fix_broken_steps.py
edits to `steps` (both the risky splitting and the safe background/
materials reclassification) — reverting to the known-good baseline that
normalise.py itself produced. Title/category/materials/tags (LLM-derived
fields) are left untouched since they were never the problem.

Usage:
    python3 scripts/revert_steps_to_raw.py --dry-run
    python3 scripts/revert_steps_to_raw.py
"""

import argparse
import importlib.util
import json
import pathlib

SOURCES_DIR = pathlib.Path("data/sources")

spec = importlib.util.spec_from_file_location("normalise", pathlib.Path("scripts/normalise.py"))
normalise = importlib.util.module_from_spec(spec)
spec.loader.exec_module(normalise)


def process(dry_run: bool, source_filter: str | None = None) -> None:
    changed = unchanged = no_raw = 0

    pattern = f"{source_filter}/normalised/*.json" if source_filter else "*/normalised/*.json"
    for norm_path in sorted(SOURCES_DIR.glob(pattern)):
        source = norm_path.parent.parent.name
        raw_path = SOURCES_DIR / source / "raw" / norm_path.name

        if not raw_path.exists():
            no_raw += 1
            continue

        try:
            normalised = json.loads(norm_path.read_text())
            raw = json.loads(raw_path.read_text())
        except Exception:
            continue

        clean_steps = normalise.steps_from_raw(raw.get("steps_raw"))
        # normalise.py's raw steps_from_raw output lacks step_id — assign it,
        # matching build_canonical's original convention.
        for i, s in enumerate(clean_steps):
            s.setdefault("substeps", [])
            s["step_id"] = i
            s.setdefault("is_critical", any(
                w in s.get("instruction", "").lower()
                for w in ["critical", "immediately", "do not", "must not"]
            ))
            s.setdefault("timers", [])

        current_steps = normalised.get("steps") or []
        if clean_steps == current_steps:
            unchanged += 1
            continue

        changed += 1
        if not dry_run:
            normalised["steps"] = clean_steps
            norm_path.write_text(json.dumps(normalised, ensure_ascii=False, indent=2))

    print(f"Changed: {changed} | Unchanged: {unchanged} | No raw counterpart (left alone): {no_raw}")
    if dry_run:
        print("(dry run — no files written)")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--source", help="Limit to one source (e.g. zenodo) — "
                         "IMPORTANT: this wipes titles/substeps back to normalise.py's "
                         "baseline for whatever it touches, so never run this on a source "
                         "that already has completed restructuring work you want to keep.")
    args = parser.parse_args()
    process(args.dry_run, args.source)


if __name__ == "__main__":
    main()
