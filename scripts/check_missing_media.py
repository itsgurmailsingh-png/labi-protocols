"""
Audit: does every protocol that TEXTUALLY references a figure/image/file
actually have that media attached?

Scans each published protocol's title + step instructions + materials for
language implying a visual/attached asset a reader would need (e.g. "see
Figure 2", "as shown in the image below", "refer to the attached file"),
and flags any such protocol whose `media` field is empty.

This is a heuristic FLAG, not a hard gate — some references are to inline
data (a numbers table described in prose) rather than a real missing image.
Report only; nothing is auto-excluded.

Usage:
    python3 scripts/check_missing_media.py
    python3 scripts/check_missing_media.py --sample 20   # print example protocol_ids
"""

import argparse
import glob
import json
import re
from collections import Counter

PATTERNS = [
    re.compile(r"\bsee\s+fig(?:ure)?\.?\s*\d", re.IGNORECASE),
    re.compile(r"\bfig(?:ure)?\.?\s*\d+\s*(?:shows|below|above|illustrates)", re.IGNORECASE),
    re.compile(r"\bas\s+shown\s+in\s+(?:the\s+)?(?:fig(?:ure)?|image|photo|diagram)", re.IGNORECASE),
    re.compile(r"\b(?:image|photo|diagram|illustration)\s+(?:below|above)", re.IGNORECASE),
    re.compile(r"\bsee\s+(?:the\s+)?(?:image|photo|diagram|illustration|attached)", re.IGNORECASE),
    re.compile(r"\brefer\s+to\s+the\s+attached", re.IGNORECASE),
    re.compile(r"\bsupplementary\s+(?:file|data|video|figure)s?\s+(?:available|attached)", re.IGNORECASE),
    re.compile(r"\bas\s+pictured\b", re.IGNORECASE),
    re.compile(r"\billustrated\s+in\b", re.IGNORECASE),
    re.compile(r"\bsee\s+video\b", re.IGNORECASE),
]


def protocol_text(d: dict) -> str:
    parts = [d.get("title", "")]
    for s in d.get("steps") or []:
        parts.append(s.get("title", ""))
        parts.append(s.get("instruction", ""))
        for sub in s.get("substeps") or []:
            parts.append(sub.get("instruction", ""))
    parts.extend(d.get("materials") or [])
    return "\n".join(p for p in parts if p)


def requires_media(text: str) -> str | None:
    for pat in PATTERNS:
        m = pat.search(text)
        if m:
            return m.group(0)
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=0)
    args = ap.parse_args()

    by_source = Counter()
    flagged_by_source = Counter()
    flagged = []

    for f in sorted(glob.glob("protocols/*.json")):
        d = json.loads(open(f).read())
        source = d.get("source_name", "unknown")
        by_source[source] += 1
        text = protocol_text(d)
        match = requires_media(text)
        if match and not d.get("media"):
            flagged_by_source[source] += 1
            flagged.append((d.get("protocol_id"), source, match, f))

    print(f"Total published protocols: {sum(by_source.values())}")
    print(f"Flagged (text implies media, none attached): {len(flagged)}\n")
    print("By source:")
    for src in sorted(by_source, key=lambda s: -flagged_by_source[s]):
        total = by_source[src]
        flagged_n = flagged_by_source[src]
        if flagged_n:
            print(f"  {src:24s} {flagged_n:5d} / {total:5d} ({flagged_n/total*100:5.1f}%)")

    if args.sample:
        print(f"\nSample ({min(args.sample, len(flagged))} of {len(flagged)}):")
        for pid, src, match, f in flagged[: args.sample]:
            print(f"  [{src}] {pid}  —  matched: {match!r}")

    with open("logs/missing_media_flagged.jsonl", "w") as out:
        for pid, src, match, f in flagged:
            out.write(json.dumps({"protocol_id": pid, "source": src, "matched_text": match}) + "\n")
    print(f"\nFull list written to logs/missing_media_flagged.jsonl")


if __name__ == "__main__":
    main()
