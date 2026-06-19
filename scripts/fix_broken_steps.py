#!/usr/bin/env python3
"""
Re-process broken protocol files in-place.
Fixes:
  1. Background sections ("Before you begin") treated as steps → description
  2. Materials sections treated as steps → materials field
  3. Mega-step: procedure crammed into one step → split into individual steps
  4. Decimal number false-positive in sub-step detection
"""
import json, re
from pathlib import Path

PROTOCOLS_DIR = Path("protocols")

BACKGROUND_HEADERS = {
    "before you begin", "background", "overview", "introduction",
    "key resources table", "resource availability",
    "significance", "abstract", "summary", "highlights",
    "validation of protocol", "limitations", "troubleshooting",
    "expected outcomes", "general notes", "notes",
}

MATERIALS_HEADERS = {
    "materials and reagents", "reagents", "materials", "equipment",
    "solutions", "recipes", "lab supplies", "laboratory supplies",
    "supplies", "buffers", "antibodies", "chemicals",
}

# Steps that are clearly procedural (start with action verb)
ACTION_VERBS = re.compile(
    r'^(add|mix|centrifuge|incubate|prepare|dissolve|transfer|wash|remove|place|'
    r'pour|heat|cool|vortex|pipette|collect|measure|resuspend|dilute|spin|store|'
    r'take|load|apply|perform|run|image|stain|block|rinse|dry|cut|punch|clean|'
    r'coat|assemble|mount|insert|fill|connect|turn|click|open|close|set|check|'
    r'confirm|ensure|secure|attach|press|align|observe|note|calculate|record|'
    r'filter|elute|pellet|decant|repeat|label|seal|shake|sonicate|degas|cast|'
    r'cure|bond|activate|purify|express|clone|transform|transfect|infect|harvest)',
    re.IGNORECASE
)

def classify_header(title: str) -> str:
    """Returns 'background', 'materials', or 'procedure'."""
    t = re.sub(r'\s+', ' ', title.lower().strip())
    t = re.sub(r'[^a-z ]', '', t)
    if any(t.startswith(h) or h in t for h in BACKGROUND_HEADERS):
        return 'background'
    if any(t.startswith(h) or h in t for h in MATERIALS_HEADERS):
        return 'materials'
    return 'procedure'


def split_inline_numbered(text: str) -> list[str]:
    """Split 'wall of text' procedure into individual steps by numbered list."""
    parts = re.split(r'(?<!\d)(?:step\s+)?(\d+)[\.:\)]\s+(?=[A-Z\w])', text, flags=re.IGNORECASE)
    if len(parts) < 5:
        return []
    steps = []
    i = 1
    while i + 1 < len(parts):
        step_text = parts[i + 1].strip()
        if len(step_text) > 15:
            steps.append(step_text)
        i += 2
    return steps if len(steps) >= 3 else []


def is_material_item(text: str) -> bool:
    """Heuristic: is this text a material/reagent item rather than a procedure step?"""
    text = text.strip()
    # Starts with action verb → procedure
    if ACTION_VERBS.match(text):
        return False
    # Contains catalog number or brand → material
    if re.search(r'catalog number|cat\.?\s*no|sigma|aldrich|thermo|millipore|abcam|bio-rad|invitrogen|neb\b|roche|qiagen|santa cruz', text, re.I):
        return True
    # Short noun phrase with no verb → likely material
    words = text.split()
    if len(words) <= 8 and not ACTION_VERBS.match(text):
        return True
    return False


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
    steps = data.get('steps') or []
    if not steps:
        return data, False

    new_steps = []
    new_materials = list(data.get('materials') or [])
    background_texts = []
    changed = False

    for step in steps:
        title = (step.get('title') or '').strip()
        instruction = (step.get('instruction') or '').strip()
        if not instruction:
            continue

        section_type = classify_header(title)

        if section_type == 'background':
            background_texts.append(instruction)
            changed = True
            continue

        if section_type == 'materials':
            # Try to split into individual material items
            items = split_inline_numbered(instruction)
            if items:
                for item in items:
                    item = item.strip()
                    if item and item not in new_materials:
                        new_materials.append(item)
            else:
                # Single material block — add as-is
                if instruction not in new_materials:
                    new_materials.append(instruction)
            changed = True
            continue

        # Procedure section — try to expand mega-step
        split = split_inline_numbered(instruction)
        if len(split) >= 3:
            changed = True
            # Check if split items are actually materials or procedure
            proc_items = []
            mat_items = []
            for s in split:
                if is_material_item(s):
                    mat_items.append(s)
                else:
                    proc_items.append(s)

            for m in mat_items:
                if m not in new_materials:
                    new_materials.append(m)

            for s in proc_items:
                new_steps.append({
                    "step_id": len(new_steps),
                    "title": infer_title(s),
                    "instruction": s,
                    "substeps": [],
                    "is_critical": any(w in s.lower() for w in ["critical", "immediately", "do not", "must not"]),
                    "timers": [],
                })
        else:
            # Check if this kept step is actually a material item
            if is_material_item(instruction) and not ACTION_VERBS.match(instruction):
                if instruction not in new_materials:
                    new_materials.append(instruction)
                changed = True
            else:
                if not title:
                    step['title'] = infer_title(instruction)
                    changed = True
                step['step_id'] = len(new_steps)
                new_steps.append(step)

    if not new_steps and steps:
        return data, False

    if background_texts and not data.get('description'):
        data['description'] = ' '.join(background_texts)[:2000]
        changed = True

    if new_materials != list(data.get('materials') or []):
        data['materials'] = new_materials[:30]  # cap at 30
        changed = True

    data['steps'] = new_steps
    return data, changed


def main():
    files = list(PROTOCOLS_DIR.glob("*.json"))
    fixed = 0
    errors = 0

    for f in files:
        try:
            data = json.loads(f.read_text())
            fixed_data, changed = fix_protocol(data)
            if changed and len(fixed_data.get('steps') or []) >= 1:
                f.write_text(json.dumps(fixed_data, ensure_ascii=False, indent=2))
                fixed += 1
        except Exception as e:
            errors += 1

    print(f"Fixed:  {fixed:,}")
    print(f"Errors: {errors}")
    print(f"Total:  {len(files):,}")


if __name__ == "__main__":
    main()
