"""
One-off backfill: fix verification_status and timers on already-normalised
files so they match the Labi app's real schema (see catalog_protocol_record.dart
and ingest_pipeline.py::score_credibility() in the labi app repo).

Rewrites data/sources/{source}/normalised/*.json in place:
  - verification_status: recomputed via score_credibility(source_url, author)
    instead of the old license-verified-based "verified"/"unverified" value.
  - steps[].timers[]: renamed duration_secs -> duration_seconds, added
    timer_id and type if missing.

Run this, then re-run merge_and_dedup.py to regenerate data/merged/ and
protocols/, then rebuild both indexes.
"""

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from normalise import score_credibility, infer_timer_type, SOURCES_DIR


def fix_timers(timers: list) -> list:
    fixed = []
    for i, t in enumerate(timers or []):
        if not isinstance(t, dict):
            continue
        duration = t.get("duration_seconds", t.get("duration_secs", 0))
        label = t.get("label", "")
        fixed.append({
            "timer_id": t.get("timer_id") or f"t{i + 1}",
            "label": label,
            "duration_seconds": duration,
            "type": t.get("type") or infer_timer_type(label),
        })
    return fixed


def main():
    changed = 0
    checked = 0
    for source_dir in sorted(SOURCES_DIR.iterdir()):
        norm_dir = source_dir / "normalised"
        if not norm_dir.exists():
            continue
        for path in norm_dir.glob("*.json"):
            checked += 1
            try:
                d = json.loads(path.read_text())
            except (json.JSONDecodeError, OSError):
                continue

            dirty = False

            new_status = score_credibility(d.get("source_url", ""), d.get("author", ""))
            if d.get("verification_status") != new_status:
                d["verification_status"] = new_status
                dirty = True

            for step in d.get("steps") or []:
                if not isinstance(step, dict):
                    continue
                old_timers = step.get("timers") or []
                if not old_timers:
                    continue
                new_timers = fix_timers(old_timers)
                if new_timers != old_timers:
                    step["timers"] = new_timers
                    dirty = True

            if dirty:
                path.write_text(json.dumps(d, indent=2, ensure_ascii=False))
                changed += 1

    print(f"Checked {checked} normalised files, updated {changed}.")


if __name__ == "__main__":
    main()
