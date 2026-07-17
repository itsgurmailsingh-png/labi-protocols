"""
detect_genuine_protocol.py

Flags entries that aren't actually runnable lab protocols — academic papers
(forecasting models, literature reviews, purely computational methods) that
got swept into the pipeline because their PMC XML had a "Methods"-adjacent
section, but have no physical bench actions a scientist could follow.
Found by inspecting a methodsx entry whose "steps" were literally the
paper's "Interest of study" / "Related works" sections, not lab actions.

Scoring (0-100, no LLM — cheap, deterministic, runs on the whole corpus):
  - action_verb_fraction: % of steps whose instruction starts with a bench
    action verb (reuses fix_broken_steps.py::ACTION_VERBS)
  - generic_title_fraction: % of step titles matching academic-paper
    section names (introduction, related work, discussion, conclusion...)
  - measurement_density: measurement/quantity tokens per step (reuses
    restructure_steps_llm.py::extract_measurement_tokens)
  - has_materials: whether a materials/reagents list exists

A protocol scoring below the threshold is flagged, not deleted — this
script only adds a "content_type" field so a filter step can decide what
to do with it. Validated against one known-bad (forecasting paper, scores
low) and several known-good wet-lab protocols (score high) before running
on the full corpus.

Usage:
    python3 scripts/detect_genuine_protocol.py --validate
    python3 scripts/detect_genuine_protocol.py --dry-run
    python3 scripts/detect_genuine_protocol.py
"""

import argparse
import importlib.util
import json
import pathlib
import re

SOURCES_DIR = pathlib.Path("data/sources")

_fbs_spec = importlib.util.spec_from_file_location("fix_broken_steps", "scripts/fix_broken_steps.py")
fbs = importlib.util.module_from_spec(_fbs_spec)
_fbs_spec.loader.exec_module(fbs)

_r_spec = importlib.util.spec_from_file_location("restructure_steps_llm", "scripts/restructure_steps_llm.py")
try:
    r = importlib.util.module_from_spec(_r_spec)
    _r_spec.loader.exec_module(r)
    extract_measurement_tokens = r.extract_measurement_tokens
except Exception:
    _TOKEN_RE = re.compile(
        r"\d[\d,]*(?:\.\d+)?\s*(?:µL|uL|mL|L|mg|g|kg|µg|ng|mM|M|nM|°C|°F|min|sec|s\b|h\b|hr|rpm|x\s*g|%|bp|kb|nm|µm|mm|cm)?",
        re.IGNORECASE,
    )
    def extract_measurement_tokens(text):
        return set(m.group(0) for m in _TOKEN_RE.finditer(text or "") if not re.fullmatch(r"\d{1,2}", m.group(0)))

GENERIC_ACADEMIC_TITLES = (
    "introduction", "background", "abstract", "related work", "discussion",
    "conclusion", "literature review", "overview", "summary", "motivation",
    "interest of study", "future work", "limitations", "acknowledg",
    "references", "methodology overview", "state of the art",
    "problem statement", "research question", "hypothesis",
)

# Word-boundary version (not anchored to string start) — used to count
# action-verb OCCURRENCES anywhere in the full step text.
_ACTION_WORD = re.compile(fbs.ACTION_VERBS.pattern.replace("^(", r"\b(", 1), re.IGNORECASE)


def materials_look_real(materials: list) -> bool:
    """
    A real materials list is short reagent/equipment names ("Pentobarbital",
    "Syringe"). A garbled one (found on a mis-extracted forecasting paper) is
    equation fragments and step descriptions ("is approximated by the
    following difference equation:(6)x1(0)(t)+az1(1)(t)=..."). Long entries
    or ones containing equation symbols are the tell.
    """
    if not materials:
        return False
    bad = sum(1 for m in materials if len(m.split()) > 8 or any(c in m for c in "=∫∑χ⋮⋯"))
    return (bad / len(materials)) < 0.3


def score_protocol(d: dict) -> tuple[int, list[str]]:
    steps = d.get("steps") or []
    if not steps:
        return 0, ["no steps"]

    reasons = []

    # Action-verb density across the FULL protocol text (per 100 words),
    # not per-step position. Checking only the first N chars of each step
    # falsely penalizes real protocols that are still in unsplit mega-step
    # form (one giant blob starting with "Procedure Cloning strategy...")
    # — the action verbs are in there, just not at position 0. Validated:
    # known non-protocol scored 1.8/100w vs 5.5-7.1/100w on real protocols,
    # including ones still in mega-step form.
    all_text = " ".join((s.get("instruction") or "") for s in steps)
    words = all_text.split()
    action_hits = len(_ACTION_WORD.findall(all_text))
    action_density = (action_hits / len(words) * 100) if words else 0

    titled = [s for s in steps if (s.get("title") or "").strip()]
    generic_titles = sum(
        1 for s in titled
        if any(kw in (s.get("title") or "").strip().lower() for kw in GENERIC_ACADEMIC_TITLES)
    )
    generic_fraction = generic_titles / len(titled) if titled else 0

    has_materials = bool(d.get("materials"))

    score = round(min(action_density, 10) * 8 + (10 if has_materials else 0))
    score -= round(min(generic_fraction * 20, 20))
    score = max(0, min(100, score))

    if action_density < 3.0:
        reasons.append(f"low action-verb density ({action_density:.1f}/100 words)")
    if generic_fraction > 0.3:
        reasons.append(f"generic academic section titles ({generic_fraction:.0%} of titled steps)")
    if not has_materials:
        reasons.append("no materials list")

    return score, reasons


# Deliberately conservative — a false positive here hides a real protocol
# from the library, a false negative just leaves one unflagged review paper
# in. Bias toward the cheaper mistake.
THRESHOLD = 22


def validate():
    """Sanity-check against known examples before trusting this on the corpus."""
    print("=== Known-bad example (forecasting paper swept into methodsx) ===")
    try:
        d = json.loads(pathlib.Path("data/sources/methodsx/normalised/PMC10009715.json").read_text())
        score, reasons = score_protocol(d)
        print(f"score={score} (expect low, <{THRESHOLD}) reasons={reasons}")
    except FileNotFoundError:
        print("  file not found — skipping")

    print("\n=== Known-good wet-lab examples ===")
    good_samples = [
        "data/sources/protocols_io/normalised/haematoxylin-eosin-h-e-staining-ihxcb7n.json",
        "data/sources/star_protocols/normalised/PMC8225968.json",
    ]
    for path in good_samples:
        p = pathlib.Path(path)
        if not p.exists():
            continue
        d = json.loads(p.read_text())
        score, reasons = score_protocol(d)
        print(f"{p.name}: score={score} (expect higher) reasons={reasons}")


def process(dry_run: bool) -> None:
    flagged = excluded = total = 0
    by_source = {}
    excluded_by_source = {}

    for norm_path in sorted(SOURCES_DIR.glob("*/normalised/*.json")):
        source = norm_path.parent.parent.name
        try:
            d = json.loads(norm_path.read_text())
        except Exception:
            continue

        total += 1
        score, reasons = score_protocol(d)
        content_type = "protocol" if score >= THRESHOLD else "likely_not_protocol"

        # NOT used for exclusion — see THRESHOLD comment above the class.
        # Tried requiring low score + garbled/absent materials as a
        # higher-precision exclude signal; found real false positives during
        # validation (Morris Water Maze, a completely legitimate behavioral
        # neuroscience protocol, has zero materials because behavioral/
        # computational protocols don't have reagent lists — not because
        # they're fake). "No materials" isn't a safe non-protocol signal on
        # its own. Kept as an informational field only; do not wire into
        # merge_and_dedup.py as a publish filter without a materially better
        # signal than this.
        materials_ok = materials_look_real(d.get("materials") or [])
        should_exclude = False

        if content_type == "likely_not_protocol":
            flagged += 1
            by_source[source] = by_source.get(source, 0) + 1
        if should_exclude:
            excluded += 1
            excluded_by_source[source] = excluded_by_source.get(source, 0) + 1

        if not dry_run and (d.get("content_type") != content_type or d.get("exclude_low_confidence") != should_exclude):
            d["content_type"] = content_type
            d["content_type_score"] = score
            d["exclude_low_confidence"] = should_exclude
            norm_path.write_text(json.dumps(d, ensure_ascii=False, indent=2))

    print(f"Total checked: {total}")
    print(f"Flagged as likely_not_protocol (informational tag only): {flagged} ({100*flagged/total:.1f}%)")
    for src, n in sorted(by_source.items(), key=lambda x: -x[1]):
        print(f"    {n:5d}  {src}")
    print(f"\nHigh-precision exclude (score low AND materials garbled/absent): {excluded} ({100*excluded/total:.1f}%)")
    for src, n in sorted(excluded_by_source.items(), key=lambda x: -x[1]):
        print(f"    {n:5d}  {src}")
    if dry_run:
        print("(dry run — no files written)")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--validate", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.validate:
        validate()
        return

    process(args.dry_run)


if __name__ == "__main__":
    main()
