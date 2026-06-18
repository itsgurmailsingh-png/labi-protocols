"""
backfill_steps_protocols_io.py

For every already-saved protocols_io raw JSON that has empty steps_raw,
fetch the full protocol detail from the individual endpoint and update in place.

Usage:
    export PROTOCOLS_IO_TOKEN="your_bearer_token"
    python3 scripts/sources/backfill_steps_protocols_io.py
"""

import json
import os
import re
import time
import pathlib
import datetime

import requests

TOKEN    = os.environ.get("PROTOCOLS_IO_TOKEN", "")
RAW_DIR  = pathlib.Path("data/sources/protocols_io/raw")
DELAY    = 0.5   # seconds between detail API calls — polite rate
PIO_BASE = "https://www.protocols.io/api/v3"

HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "User-Agent": "LabiApp/1.0 (mailto:support@labi.app)",
}


def clean_html(raw: str) -> str:
    text = re.sub(r"<[^>]+>", " ", str(raw))
    return re.sub(r"\s+", " ", text).strip()


def flatten_steps(steps_data) -> list:
    """Convert protocols.io steps → list of {title, instruction} dicts."""
    if not steps_data:
        return []
    result = []
    for step in steps_data:
        if isinstance(step, str):
            result.append({"title": "", "instruction": step})
            continue

        title = clean_html(step.get("title") or "")

        # Instruction: try top-level 'step' field (pre-rendered HTML) first
        top_step = step.get("step") or ""
        if top_step:
            instruction = clean_html(top_step)
            if instruction:
                result.append({"title": title, "instruction": instruction})
                continue

        # Fall back to components → source → description
        components = step.get("components") or []
        parts = []
        for comp in components:
            source = comp.get("source") or {}
            desc = (
                source.get("description")
                or source.get("body")
                or comp.get("description")
                or comp.get("body")
                or ""
            )
            desc = clean_html(desc)
            if desc:
                parts.append(desc)
        instruction = " ".join(parts).strip()
        if instruction:
            result.append({"title": title, "instruction": instruction})
    return result


def extract_stats(proto: dict) -> dict:
    """Extract quality signals from protocol detail response."""
    s = proto.get("stats") or {}
    return {
        "views":     int(s.get("number_of_views") or s.get("views") or 0),
        "runs":      int(s.get("number_of_runs") or s.get("runs") or 0),
        "bookmarks": int(s.get("number_of_bookmarks") or s.get("bookmarks") or 0),
        "comments":  int(s.get("number_of_comments") or s.get("number_of_protocol_comments") or 0),
        "forks":     int((s.get("number_of_forks") or {}).get("public", 0) if isinstance(s.get("number_of_forks"), dict) else s.get("number_of_forks") or 0),
    }


def fetch_detail(source_id: str) -> dict | None:
    """Fetch the individual protocol endpoint.
    Returns dict with keys: steps, stats, peer_reviewed — or None on failure.
    """
    url = f"{PIO_BASE}/protocols/{source_id}"
    try:
        r = requests.get(url, headers=HEADERS, timeout=30)
        if r.status_code == 401:
            print("[FATAL] 401 Unauthorized — token expired or invalid.")
            raise SystemExit(1)
        if r.status_code == 404:
            return None
        if r.status_code != 200:
            print(f"  [WARN] HTTP {r.status_code} for protocol {source_id}")
            return None
        data = r.json()
        proto = data.get("protocol") or data.get("item") or data
        return {
            "steps":         flatten_steps(proto.get("steps") or []),
            "stats":         extract_stats(proto),
            "peer_reviewed": bool(proto.get("peer_reviewed", False)),
        }
    except requests.RequestException as e:
        print(f"  [ERR] {e}")
        return None


def main():
    if not TOKEN:
        print("[FATAL] PROTOCOLS_IO_TOKEN not set.")
        raise SystemExit(1)

    files = sorted(RAW_DIR.glob("*.json"))
    total = len(files)
    # Backfill needed if steps_raw empty OR stats is None/missing
    needs_backfill = []
    for f in files:
        d = json.loads(f.read_text())
        if not d.get("steps_raw") or d.get("stats") is None:
            needs_backfill.append(f)
    already_done = total - len(needs_backfill)

    print(f"\nBackfill protocols.io steps + stats + peer_reviewed")
    print(f"  Total saved       : {total}")
    print(f"  Already complete  : {already_done}")
    print(f"  Need backfill     : {len(needs_backfill)}\n")

    updated = 0
    failed  = 0
    not_found = 0

    for i, fpath in enumerate(needs_backfill, 1):
        data = json.loads(fpath.read_text())
        sid   = data["source_id"]
        title = data.get("title", "")[:50]

        detail = fetch_detail(sid)
        time.sleep(DELAY)

        if detail is None:
            not_found += 1
            print(f"  [{i}/{len(needs_backfill)}] NOT FOUND  {sid}: {title}")
            continue

        steps = detail["steps"]
        stats = detail["stats"]
        peer  = detail["peer_reviewed"]

        # Always update stats + peer_reviewed even if no steps
        data["stats"]         = stats
        data["peer_reviewed"] = peer
        data["fetched_at"]    = datetime.datetime.utcnow().isoformat() + "Z"

        if steps:
            data["steps_raw"] = steps
            updated += 1
            print(f"  [{i}/{len(needs_backfill)}] UPDATED ✅  {sid}: {title} — {len(steps)} steps | runs={stats.get('runs',0)} peer={peer}")
        else:
            failed += 1
            print(f"  [{i}/{len(needs_backfill)}] NO STEPS   {sid}: {title} | runs={stats.get('runs',0)} peer={peer}")

        fpath.write_text(json.dumps(data, ensure_ascii=False, indent=2))

    print(f"\n=== Done ===")
    print(f"  Updated   : {updated}")
    print(f"  No steps  : {failed}")
    print(f"  Not found : {not_found}")


if __name__ == "__main__":
    main()
