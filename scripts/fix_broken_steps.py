#!/usr/bin/env python3
"""
Re-process broken protocol files in-place.
Fixes two issues:
  1. Background sections ("Before you begin", "Overview" etc.) treated as steps
  2. Mega-step: entire procedure crammed into one step as inline numbered list
"""
import json, re
from pathlib import Path

PROTOCOLS_DIR = Path("protocols")

# Section headers that are BACKGROUND, not procedure steps
BACKGROUND_HEADERS = {
    "before you begin", "background", "overview", "introduction",
    "key resources table", "resource availability", "method details",
    "significance", "abstract", "summary", "highlights",
    "validation of protocol", "limitations", "troubleshooting",
    "expected outcomes", "general notes", "notes",
}

def is_background_header(title: str) -> bool:
    t = re.sub(r'\s+', ' ', title.lower().strip())
    t = re.sub(r'[^a-z ]', '', t)
    return any(t.startswith(h) or h in t for h in BACKGROUND_HEADERS)


def split_inline_numbered(text: str) -> list[str]:
    """
    Split text containing inline numbered steps like:
    "1. Do this 2. Do that 3. Do other"
    Returns list of instruction strings, or [] if pattern not found.
    """
    # Match patterns: "1. " "1) " "Step 1: " "Step 1. "
    parts = re.split(r'(?<!\d)(?:step\s+)?(\d+)[\.:\)]\s+(?=[A-Z\w])', text, flags=re.IGNORECASE)
    # re.split with groups returns [pre, n, text, n, text, ...]
    if len(parts) < 5:
        return []

    steps = []
    # parts[0] is text before first number, then alternating: number, text
    i = 1
    while i + 1 < len(parts):
        step_text = parts[i + 1].strip()
        if len(step_text) > 15:
            steps.append(step_text)
        i += 2

    return steps if len(steps) >= 3 else []


def infer_title(instruction: str) -> str:
    if not instruction:
        return "Step"
    m = re.match(r'^([^:\n]{3,60}):\s+\S', instruction)
    if m:
        t = m.group(1).strip()
        if len(t.split()) <= 10:
            return t
    m = re.match(r'^([A-Z][A-Z\s&/\-]{4,50})\s*[\n\.]', instruction)
    if m:
        return m.group(1).strip().title()
    first_sent = re.split(r'[.!?\n]', instruction)[0].strip()
    words = first_sent.split()
    if 3 <= len(words) <= 10:
        return first_sent
    return ' '.join(words[:6]) + ('...' if len(words) > 6 else '')


def fix_protocol(data: dict) -> tuple[dict, bool]:
    """Fix a protocol dict. Returns (fixed_dict, was_changed)."""
    steps = data.get('steps') or []
    if not steps:
        return data, False

    new_steps = []
    background_texts = []
    changed = False

    for step in steps:
        title = (step.get('title') or '').strip()
        instruction = (step.get('instruction') or '').strip()

        # 1. Move background sections to description
        if is_background_header(title):
            background_texts.append(instruction)
            changed = True
            continue

        # 2. Try to split mega-step with inline numbered list
        split = split_inline_numbered(instruction)
        if len(split) >= 3:
            changed = True
            for j, s in enumerate(split):
                new_steps.append({
                    "step_id": len(new_steps),
                    "title": infer_title(s),
                    "instruction": s,
                    "substeps": [],
                    "is_critical": any(w in s.lower() for w in ["critical", "immediately", "do not", "must not"]),
                    "timers": [],
                })
        else:
            # Keep as-is but fix title if missing
            if not title:
                step['title'] = infer_title(instruction)
                changed = True
            step['step_id'] = len(new_steps)
            new_steps.append(step)

    if not new_steps and steps:
        # Don't wipe all steps if nothing survived
        return data, False

    if background_texts and not data.get('description'):
        data['description'] = ' '.join(background_texts)[:2000]

    data['steps'] = new_steps
    return data, changed


def main():
    files = list(PROTOCOLS_DIR.glob("*.json"))
    fixed = 0
    skipped = 0
    errors = 0

    for f in files:
        try:
            data = json.loads(f.read_text())
            fixed_data, changed = fix_protocol(data)
            if changed:
                # Only write if result has at least 1 step
                if len(fixed_data.get('steps') or []) >= 1:
                    f.write_text(json.dumps(fixed_data, ensure_ascii=False, indent=2))
                    fixed += 1
                else:
                    skipped += 1
        except Exception as e:
            errors += 1

    print(f"Fixed:   {fixed:,}")
    print(f"Skipped: {skipped} (result had 0 steps)")
    print(f"Errors:  {errors}")
    print(f"Total:   {len(files):,}")


if __name__ == "__main__":
    main()
