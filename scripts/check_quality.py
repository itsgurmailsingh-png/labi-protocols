"""
Quality check for normalised protocols.
Usage:
    python3 scripts/check_quality.py                   # all sources
    python3 scripts/check_quality.py --source zenodo   # one source
    python3 scripts/check_quality.py --fix             # delete bad files for reprocessing
"""
import json, pathlib, random, re, sys

SOURCES = ["protocols_io", "pubmed_central", "zenodo", "bio_protocol", "figshare", "openwetware", "vendor"]
FIX_MODE = "--fix" in sys.argv

# Parse --source flag
source_arg = None
for i, arg in enumerate(sys.argv):
    if arg == "--source" and i + 1 < len(sys.argv):
        source_arg = sys.argv[i + 1]

sources_to_check = [source_arg] if source_arg else SOURCES

# Collect all normalised files across requested sources
all_files = []
for src in sources_to_check:
    nd = pathlib.Path(f"data/sources/{src}/normalised")
    if nd.exists():
        src_files = list(nd.glob("*.json"))
        all_files.extend(src_files)

files = all_files
nd = pathlib.Path("data/sources/protocols_io/normalised")  # kept for fix-mode compat

def is_junk(text: str) -> bool:
    """Returns True if instruction text is garbage."""
    t = text.strip()
    if not t:
        return True
    if len(t) < 10:
        return True
    # Only symbols/numbers, no real words
    if not re.search(r"[a-zA-Z]{3,}", t):
        return True
    # HTML artifact leftover
    if re.search(r"&[a-z]+;|<[a-z]", t, re.I):
        return True
    # dict-as-string corruption
    if t.startswith("{'title'") or t.startswith('{"title"'):
        return True
    return False

issues = {
    "corrupted_steps": [],   # dict serialised as string
    "junk_instructions": [], # instructions too short/garbage
    "no_steps": [],          # protocol has 0 steps despite having raw steps
    "bad_time": [],          # estimated_time_mins is null, 0, or >2000
    "no_materials": [],      # empty materials list
    "bad_category": [],      # category is "Other" when it probably shouldn't be
}
good = []

for f in files:
    try:
        d = json.loads(f.read_text())
    except Exception:
        issues["corrupted_steps"].append(f.name)
        continue

    steps = d.get("steps", [])
    file_issues = []

    # 1. dict-as-string corruption
    if steps and steps[0].get("instruction", "").startswith("{'title'"):
        issues["corrupted_steps"].append(f.name)
        file_issues.append("corrupted")

    # 2. Junk instructions — check first 3 steps
    junk_count = sum(1 for s in steps[:3] if is_junk(s.get("instruction", "")))
    if junk_count >= 2:
        issues["junk_instructions"].append(f.name)
        file_issues.append("junk_instructions")

    # 3. No steps
    if not steps:
        issues["no_steps"].append(f.name)
        file_issues.append("no_steps")

    # 4. Bad time estimate
    t = d.get("estimated_time_mins")
    if t is None or t == 0 or t > 2000:
        issues["bad_time"].append(f.name)

    # 5. No materials
    if not d.get("materials"):
        issues["no_materials"].append(f.name)

    if not file_issues:
        good.append(f.name)

total = len(files)
print(f"\n── Quality Report ({total} files) ──")
print(f"  ✓ Clean:             {len(good)}")
print(f"  ✗ Corrupted steps:   {len(issues['corrupted_steps'])}")
print(f"  ✗ Junk instructions: {len(issues['junk_instructions'])}")
print(f"  ⚠ No steps:          {len(issues['no_steps'])}")
print(f"  ⚠ Bad time estimate: {len(issues['bad_time'])}")
print(f"  ⚠ No materials:      {len(issues['no_materials'])}")

# Build a map of filename → full path for fix mode and spot checks
file_path_map = {f.name: f for f in files}

# Show examples of junk
if issues["junk_instructions"]:
    print("\n── Junk Instruction Examples ──")
    for name in issues["junk_instructions"][:3]:
        fpath = file_path_map.get(name)
        if not fpath: continue
        d = json.loads(fpath.read_text())
        steps = d.get("steps", [])
        print(f"\n  {name}")
        for s in steps[:2]:
            print(f"    instr: {repr(s.get('instruction',''))[:120]}")

# Fix: delete corrupted and junk files so they get reprocessed
if FIX_MODE:
    to_delete = set(issues["corrupted_steps"]) | set(issues["junk_instructions"])
    deleted = 0
    for name in to_delete:
        fpath = file_path_map.get(name)
        if fpath and fpath.exists():
            fpath.unlink()
            deleted += 1
    print(f"\n  Deleted {deleted} bad files — rerun normalise to reprocess them")

# Spot-check 3 random clean files
if good:
    print("\n── Spot Check (3 random clean files) ──")
    for name in random.sample(good, min(3, len(good))):
        fpath = file_path_map.get(name)
        if not fpath: continue
        d = json.loads(fpath.read_text())
        steps = d.get("steps", [])
        s0 = steps[0] if steps else {}
        print(f"\n  {name}")
        print(f"    title:    {d.get('title','')[:70]}")
        print(f"    category: {d.get('category','')} | time: {d.get('estimated_time_mins')} mins | steps: {len(steps)}")
        print(f"    step[0] title: {repr(s0.get('title',''))[:50]}")
        print(f"    step[0] instr: {repr(s0.get('instruction',''))[:120]}")
