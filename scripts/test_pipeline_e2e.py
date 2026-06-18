"""
test_pipeline_e2e.py — End-to-end pipeline test on N protocols.

Picks N raw protocols that have steps, runs them through every layer,
and asserts the output is correct at each stage. Fails loudly if anything
is wrong. Only AFTER this passes should you run the full pipeline.

Usage:
    python3 scripts/test_pipeline_e2e.py          # test 10 protocols
    python3 scripts/test_pipeline_e2e.py --n 1    # test 1 protocol
"""

import argparse
import json
import pathlib
import re
import shutil
import sys
import tempfile
import datetime

# ── Locate scripts relative to this file ──────────────────────────────────────
REPO = pathlib.Path(__file__).parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import normalise as norm_mod
import merge_and_dedup as merge_mod

RAW_DIR = REPO / "data" / "sources" / "protocols_io" / "raw"

PASS = "✅"
FAIL = "❌"
WARN = "⚠️ "


def find_raw_with_steps(n: int) -> list[pathlib.Path]:
    """Return up to n raw JSON files that have non-empty steps_raw."""
    found = []
    for f in sorted(RAW_DIR.glob("*.json")):
        try:
            d = json.loads(f.read_text())
            if d.get("steps_raw") and len(d["steps_raw"]) > 0:
                found.append(f)
                if len(found) == n:
                    break
        except Exception:
            continue
    return found


def check(condition: bool, msg_pass: str, msg_fail: str) -> bool:
    if condition:
        print(f"  {PASS} {msg_pass}")
    else:
        print(f"  {FAIL} {msg_fail}")
    return condition


def test_raw(path: pathlib.Path) -> bool:
    """Assert raw protocol has expected fields and steps."""
    print(f"\n── RAW: {path.name} ──")
    ok = True
    try:
        d = json.loads(path.read_text())
    except Exception as e:
        print(f"  {FAIL} Cannot parse JSON: {e}")
        return False

    ok &= check(bool(d.get("title")), "Has title", f"Missing title")
    ok &= check(bool(d.get("source_id")), "Has source_id", "Missing source_id")
    ok &= check(bool(d.get("license_verified")), "License verified=True", "license_verified is False/missing")
    ok &= check(bool(d.get("license_url")), "Has license_url", "Missing license_url")
    steps = d.get("steps_raw", [])
    ok &= check(len(steps) > 0, f"Has {len(steps)} steps_raw", "steps_raw is empty — backfill not run?")
    if steps:
        ok &= check(isinstance(steps[0], str) and len(steps[0]) > 5,
                    f"Step 1 has text: \"{steps[0][:60]}\"",
                    f"Step 1 is empty or malformed: {repr(steps[0])}")
    return ok


def test_normalised(raw_path: pathlib.Path, out_dir: pathlib.Path) -> bool:
    """Run normalise on one raw file and assert output."""
    print(f"\n── LAYER 2 NORMALISE ──")
    norm_out = out_dir / raw_path.name
    if norm_out.exists():
        norm_out.unlink()

    # Patch normalise module to use our test dirs
    import importlib
    norm_mod.SOURCES_DIR = out_dir.parent.parent / "sources"

    # Manually run normalise on the single file
    try:
        raw = json.loads(raw_path.read_text())
    except Exception as e:
        print(f"  {FAIL} Cannot read raw: {e}")
        return False

    llm = norm_mod.llm_normalise(raw)
    if llm:
        print(f"  {PASS} LLM normalisation succeeded")
    else:
        print(f"  {WARN} LLM not available — using regex fallback")

    canonical = norm_mod.build_canonical(raw, llm)
    norm_out.parent.mkdir(parents=True, exist_ok=True)
    norm_out.write_text(json.dumps(canonical, ensure_ascii=False, indent=2))

    ok = True
    ok &= check(bool(canonical.get("protocol_id")), f"protocol_id: {canonical.get('protocol_id')}", "Missing protocol_id")
    ok &= check(bool(canonical.get("title")), f"title: {canonical.get('title')[:60]}", "Missing title")
    ok &= check(canonical.get("license_verified") is True, "license_verified=True", "license_verified not True")
    ok &= check(canonical.get("source_name") == "protocols_io", "source_name=protocols_io", f"Wrong source_name: {canonical.get('source_name')}")
    steps = canonical.get("steps", [])
    ok &= check(len(steps) > 0, f"{len(steps)} structured steps", "steps list is empty")
    if steps:
        s = steps[0]
        ok &= check(isinstance(s, dict), "Step is a dict", f"Step is not dict: {type(s)}")
        ok &= check("instruction" in s, f"Step has instruction: \"{str(s.get('instruction',''))[:60]}\"", "Step missing instruction field")
        ok &= check("step_id" in s, "Step has step_id", "Step missing step_id")
        ok &= check("is_critical" in s, "Step has is_critical", "Step missing is_critical")
        ok &= check("timers" in s, "Step has timers", "Step missing timers")
    ok &= check(canonical.get("category") in norm_mod.CATEGORIES,
                f"category: {canonical.get('category')}", f"Invalid category: {canonical.get('category')}")
    return ok


def test_merge_dedup(normalised_files: list[pathlib.Path], out_dir: pathlib.Path) -> bool:
    """Run merge+dedup on test protocols and assert outputs."""
    print(f"\n── LAYER 3 MERGE+DEDUP ──")
    merged_dir = out_dir / "merged"
    protocols_dir = out_dir / "protocols"
    merged_dir.mkdir(parents=True, exist_ok=True)
    protocols_dir.mkdir(parents=True, exist_ok=True)

    # Load normalised
    protocols = []
    for f in normalised_files:
        try:
            d = json.loads(f.read_text())
            protocols.append(d)
        except Exception as e:
            print(f"  {WARN} Could not load {f.name}: {e}")

    ok = True
    ok &= check(len(protocols) > 0, f"Loaded {len(protocols)} normalised protocols", "No protocols loaded")
    if not ok:
        return False

    groups, true_dups, ver_groups = merge_mod.group_protocols(protocols)
    total_written = sum(len(g) for g in groups)
    print(f"  {PASS} Grouping ran: {len(protocols)} in → {len(groups)} families, {true_dups} true dups removed, {ver_groups} version groups")

    # Write outputs to test dirs using merge_mod's write logic
    import re as _re
    for group in groups:
        parent = group[0]
        parent_pid = parent.get("protocol_id") or merge_mod.slugify(parent.get("title", "unknown"))
        for i, protocol in enumerate(group):
            out = merge_mod.build_output(protocol, None if i == 0 else parent_pid, len(group) if i == 0 else None)
            pid = out["protocol_id"]
            if i > 0:
                src = _re.sub(r"[^a-z0-9]", "", protocol.get("source_name", ""))[:8]
                pid = f"{parent_pid}_v{i}_{src}"
                out["protocol_id"] = pid
            path = merged_dir / f"{pid}.json"
            path.write_text(json.dumps(out, ensure_ascii=False, indent=2))
            shutil.copy2(path, protocols_dir / f"{pid}.json")

    written = list(protocols_dir.glob("*.json"))
    ok &= check(len(written) > 0, f"{len(written)} protocols written to protocols/", "Nothing written to protocols/")

    # Validate one output file
    if written:
        sample = json.loads(written[0].read_text())
        print(f"\n  Validating final output: {written[0].name}")
        ok &= check(bool(sample.get("protocol_id")),       f"protocol_id: {sample.get('protocol_id')}", "Missing protocol_id")
        ok &= check(bool(sample.get("title")),             f"title: {sample.get('title')[:60]}", "Missing title")
        ok &= check(bool(sample.get("steps")),             f"{len(sample.get('steps',[]))} steps in final output", "steps empty in final output")
        ok &= check(bool(sample.get("license")),           f"license: {sample.get('license')}", "Missing license")
        ok &= check(sample.get("license_verified") is True,"license_verified=True in final output", "license_verified not True in final output")
        ok &= check(bool(sample.get("source_name")),       f"source_name: {sample.get('source_name')}", "Missing source_name")
        ok &= check(bool(sample.get("source_url")),        f"source_url present", "Missing source_url — needed for attribution")
        ok &= check(bool(sample.get("doi")),               f"doi: {sample.get('doi')}", "Missing doi — needed for citation")
        ok &= check(bool(sample.get("author")),            f"author: {sample.get('author')}", "Missing author")
        ok &= check("stats" in sample,                     f"stats: {sample.get('stats')}", "Missing stats — needed for quality ranking")
        ok &= check("peer_reviewed" in sample,             f"peer_reviewed: {sample.get('peer_reviewed')}", "Missing peer_reviewed flag")
        ok &= check("quality_score" in sample,             f"quality_score: {sample.get('quality_score')}", "Missing quality_score")
        ok &= check("parent_protocol_id" in sample,        f"parent_protocol_id: {sample.get('parent_protocol_id')}", "Missing parent_protocol_id field")
        ok &= check("version_count" in sample,             f"version_count: {sample.get('version_count')}", "Missing version_count field")

    return ok


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--n", type=int, default=10, help="Number of protocols to test (default 10)")
    args = parser.parse_args()

    print(f"\n{'='*60}")
    print(f"  LABI PIPELINE END-TO-END TEST  (n={args.n})")
    print(f"{'='*60}")

    # Find raw protocols with steps
    raw_files = find_raw_with_steps(args.n)
    if not raw_files:
        print(f"\n{FAIL} No raw protocols with steps found. Run backfill first:\n"
              f"  python3 scripts/sources/backfill_steps_protocols_io.py")
        sys.exit(1)

    print(f"\nFound {len(raw_files)} raw protocols with steps to test.")

    # Use a temp dir for test outputs — never touches real data
    with tempfile.TemporaryDirectory(prefix="labi_test_") as tmp:
        tmp = pathlib.Path(tmp)
        norm_out_dir = tmp / "sources" / "protocols_io" / "normalised"
        norm_out_dir.mkdir(parents=True)

        all_passed = True

        # ── Layer 1: Raw validation ──
        print(f"\n{'─'*40}")
        print(f"  LAYER 1: RAW DATA VALIDATION")
        print(f"{'─'*40}")
        for f in raw_files:
            if not test_raw(f):
                all_passed = False

        # ── Layer 2: Normalise ──
        print(f"\n{'─'*40}")
        print(f"  LAYER 2: NORMALISATION")
        print(f"{'─'*40}")
        for f in raw_files:
            if not test_normalised(f, norm_out_dir):
                all_passed = False

        # ── Layer 3: Merge + Dedup ──
        print(f"\n{'─'*40}")
        print(f"  LAYER 3: MERGE + DEDUP")
        print(f"{'─'*40}")
        norm_files = list(norm_out_dir.glob("*.json"))
        if not test_merge_dedup(norm_files, tmp):
            all_passed = False

    # ── Final verdict ──
    print(f"\n{'='*60}")
    if all_passed:
        print(f"  {PASS} ALL TESTS PASSED — safe to run full pipeline")
    else:
        print(f"  {FAIL} TESTS FAILED — fix issues above before running full pipeline")
    print(f"{'='*60}\n")

    sys.exit(0 if all_passed else 1)


if __name__ == "__main__":
    main()
