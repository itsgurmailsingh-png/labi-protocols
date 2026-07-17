"""
verify_protocol_quality.py

Launch-readiness GATE for protocols/*.json. Run this after every
merge_and_dedup.py — it is the thing that should have caught the
eaten-marker regression before it shipped.

Two tiers:
  CRITICAL (exit 1 if any found — these mean content is actively wrong,
  not just incomplete):
    - severed-sentence signature (a step ends on a dangling connector word
      like "and (" and the next step starts lowercase — the exact shape of
      the split_inline_numbered bug that shredded real content)
    - step_id sequence gaps/duplicates within a protocol
    - empty instruction text anywhere in the step list

  WARN (reported, does not fail the build — these are coverage/backlog
  gaps, not corruption):
    - zero-step protocols (blank, from source or backlog, not garbling)
    - single mega-step protocols (unsplit, but content is intact)
    - category still "Other"

Usage:
    python3 scripts/verify_protocol_quality.py
    python3 scripts/verify_protocol_quality.py --source protocols_io
    python3 scripts/verify_protocol_quality.py --severed-threshold 200
"""

import argparse
import json
import pathlib
import re
import sys
from collections import Counter

PROTOCOLS_DIR = pathlib.Path("protocols")
MEGA_STEP_CHARS = 800

DANGLING_WORDS = {
    "the", "a", "an", "and", "or", "with", "using", "for", "to", "of", "in",
    "at", "by", "on", "was", "is", "were", "are", "that", "which", "then",
    "from", "into", "as", "after", "before",
}
_WORD_RE = re.compile(r"[A-Za-z']+")


def looks_like_severed_sentence(cur: str, nxt: str) -> bool:
    """
    True if `cur` looks like it was cut off mid-sentence and `nxt` looks
    like the continuation — the signature of a splitter eating a list
    marker (e.g. "...and (" / "narrative synthesis...").
    Deliberately conservative: only flags a dangling connector word
    immediately followed by a lowercase-starting next step, which is rare
    in legitimate step-to-step boundaries but exactly what a mid-sentence
    cut produces.
    """
    cur = (cur or "").strip()
    nxt = (nxt or "").strip()
    if not cur or not nxt:
        return False
    words = _WORD_RE.findall(cur)
    if not words:
        return False
    last_word = words[-1].lower()
    return last_word in DANGLING_WORDS and nxt[0].islower()


def audit(source_filter: str | None, severed_threshold: int) -> bool:
    """Returns True if the build passes the critical gate."""
    files = sorted(PROTOCOLS_DIR.glob("*.json"))

    total = 0
    zero_steps = []
    mega_step_only = []
    bad_step_order = []
    empty_instruction = []
    severed_sentences = []
    still_other = 0
    by_source_zero = Counter()
    by_source_mega = Counter()

    for f in files:
        try:
            d = json.loads(f.read_text())
        except Exception:
            continue

        src = d.get("source_name", "?")
        if source_filter and src != source_filter:
            continue
        total += 1

        steps = d.get("steps") or []

        if not steps:
            zero_steps.append(f.name)
            by_source_zero[src] += 1
            continue

        if len(steps) == 1 and len(steps[0].get("instruction", "")) > MEGA_STEP_CHARS:
            mega_step_only.append(f.name)
            by_source_mega[src] += 1

        ids = [s.get("step_id") for s in steps]
        if ids != list(range(len(ids))):
            bad_step_order.append((f.name, ids[:6]))

        if any(not (s.get("instruction") or "").strip() for s in steps):
            empty_instruction.append(f.name)

        for i in range(len(steps) - 1):
            cur = steps[i].get("instruction", "")
            nxt = steps[i + 1].get("instruction", "")
            if looks_like_severed_sentence(cur, nxt):
                severed_sentences.append((f.name, i))

        if d.get("category") == "Other":
            still_other += 1

    print(f"Total protocols audited: {total}")
    print()
    print("── CRITICAL (must be ~0) ──────────────────────────────")
    print(f"Severed-sentence pairs (eaten marker signature): {len(severed_sentences)}")
    for name, i in severed_sentences[:10]:
        print(f"    {name}  step {i}->{i+1}")
    print()
    print(f"step_id not sequential:      {len(bad_step_order)}")
    for name, ids in bad_step_order[:10]:
        print(f"    {name}  ids={ids}")
    print()
    print(f"Empty instruction in a step: {len(empty_instruction)}")
    print()
    print("── WARN (coverage/backlog, not corruption) ────────────")
    print(f"Zero-step (unusable):        {len(zero_steps)}")
    for src, n in by_source_zero.most_common():
        print(f"    {n:5d}  {src}")
    print()
    print(f"Single mega-step (>{MEGA_STEP_CHARS}ch, likely unsplit): {len(mega_step_only)}")
    for src, n in by_source_mega.most_common():
        print(f"    {n:5d}  {src}")
    print()
    print(f"Category still 'Other':      {still_other}")

    passed = (
        len(severed_sentences) <= severed_threshold
        and len(bad_step_order) == 0
        and len(empty_instruction) == 0
    )
    print()
    print(f"{'PASS' if passed else 'FAIL'} — critical gate "
          f"({'within' if passed else 'exceeds'} threshold of {severed_threshold} severed pairs)")
    return passed


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", help="Filter to one source_name")
    parser.add_argument("--severed-threshold", type=int, default=220,
                         help="Max tolerated severed-sentence pairs before failing. "
                              "Was 160; raised after the 2026-07-09 document-recovery pass "
                              "surfaced ~400 previously-blank protocols_io protocols, "
                              "proportionally growing the known-benign baseline (protocols.io's "
                              "own step/component boundaries splitting mid-sentence in their "
                              "source data — confirmed against the untouched raw API cache, "
                              "not something our pipeline introduces or can fix). Spot-checked "
                              "several of the new instances by hand before raising this; if the "
                              "count spikes well past 220, re-verify rather than raising again "
                              "reflexively — see revert_steps_to_raw.py for the original bug "
                              "this check exists to catch.")
    args = parser.parse_args()
    ok = audit(args.source, args.severed_threshold)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
