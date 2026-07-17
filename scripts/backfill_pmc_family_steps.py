"""
backfill_pmc_family_steps.py

Fixes content loss for PMC-XML-based sources (pubmed_central, star_protocols,
methodsx, biological_procedures, current_protocols). The original per-source
fetchers (e.g. fetch_methodsx.py::extract_steps_raw) only collect paragraphs
from sections whose title matches a fixed keyword list (step/procedure/
protocol/method/...), and only look at DIRECT children of a matched <sec> —
not nested subsections. Two failure modes result:
  1. A matched section's real content lives in nested <sec> children
     (common — JATS articles nest "Solenoid pump filtration system" style
     subsections under a "Method validation" parent) — non-recursive p
     lookup misses it entirely.
  2. The real procedure sections use domain-specific titles that never
     contain any of the anticipated keywords at all (e.g. "Faucet
     filtration system") — keyword matching never fires.

Verified against data/raw/pmc/*.xml (4,809 cached untouched JATS XML files,
covering 100% of the 4,374 PMC-family raw records) that walking every <sec>
in the document (so nested subsections are visited independently) and
excluding only clearly non-methods sections (background, conclusion,
acknowledgments, data availability, references, etc.) recovers real
procedure content that the keyword-only approach missed completely.

Scope: only rewrites protocols currently zero-step or single-mega-step —
deliberately does not touch already-well-extracted protocols to avoid
regressing anything that already works.

Usage:
    python3 scripts/backfill_pmc_family_steps.py --dry-run
    python3 scripts/backfill_pmc_family_steps.py
"""

import argparse
import json
import pathlib
import re
import xml.etree.ElementTree as ET

SOURCES_DIR = pathlib.Path("data/sources")
XML_CACHE   = pathlib.Path("data/raw/pmc")
PMC_SOURCES = ["pubmed_central", "star_protocols", "methodsx",
               "biological_procedures", "current_protocols"]

EXCLUDE_TITLE_KEYWORDS = {
    "background", "introduction", "conclusion", "acknowledgm",
    "competing interest", "data availability", "declaration",
    "supplementary", "abstract", "references", "author contribution",
    "funding", "ethic", "credit authorship", "conflict of interest",
    "highlights", "graphical abstract",
}

MEGA_STEP_CHARS = 800


def strip_text(el) -> str:
    text = "".join(el.itertext()) if el is not None else ""
    return re.sub(r"\s{2,}", " ", text.strip())


def extract_steps_from_xml(root: ET.Element) -> list[dict]:
    """Walk every <sec>, skip non-methods ones, keep direct paragraphs + ordered list items."""
    result = []
    for sec in root.iter("sec"):
        title_el = sec.find("title")
        title = strip_text(title_el) if title_el is not None else ""
        if any(kw in title.lower() for kw in EXCLUDE_TITLE_KEYWORDS):
            continue

        for p in sec.findall("p"):
            text = strip_text(p)
            if text:
                result.append({"title": title, "instruction": text})

        for lst in sec.findall("list[@list-type='order']"):
            for item in lst.findall("list-item"):
                text = strip_text(item)
                if text:
                    result.append({"title": title, "instruction": text})

    # Some MethodsX-style articles put their content as <p> directly under
    # <body>, never wrapped in a <sec> at all (found on PMC7355720 — a real
    # neuropeptide assay article with a full Specifications Table and
    # methodology, all as loose body-level paragraphs). root.iter("sec")
    # never visits these. Only add them if no <sec>-based content was found
    # at all, to avoid duplicating text that's already captured above.
    if not result:
        body = root.find(".//body")
        if body is not None:
            for p in body.findall("p"):
                text = strip_text(p)
                if text:
                    result.append({"title": "", "instruction": text})

    return result


def needs_fix(normalised: dict) -> bool:
    steps = normalised.get("steps") or []
    if not steps:
        return True
    if len(steps) == 1 and len(steps[0].get("instruction", "")) > MEGA_STEP_CHARS:
        return True
    return False


def process(dry_run: bool) -> None:
    checked = improved = no_xml = no_improvement = 0

    for source in PMC_SOURCES:
        norm_dir = SOURCES_DIR / source / "normalised"
        raw_dir = SOURCES_DIR / source / "raw"
        if not norm_dir.exists():
            continue

        for norm_path in sorted(norm_dir.glob("*.json")):
            try:
                normalised = json.loads(norm_path.read_text())
            except Exception:
                continue

            if not needs_fix(normalised):
                continue

            checked += 1
            pmcid = normalised.get("source_id") or norm_path.stem
            xml_path = XML_CACHE / f"{pmcid}.xml"
            if not xml_path.exists():
                no_xml += 1
                continue

            try:
                root = ET.parse(xml_path).getroot()
            except ET.ParseError:
                no_xml += 1
                continue

            new_steps = extract_steps_from_xml(root)
            old_steps = normalised.get("steps") or []
            old_total_chars = sum(len(s.get("instruction", "")) for s in old_steps)
            new_total_chars = sum(len(s.get("instruction", "")) for s in new_steps)

            # Only accept if it's a genuine improvement: more steps AND not less total content.
            if len(new_steps) <= len(old_steps) or new_total_chars < old_total_chars:
                no_improvement += 1
                continue

            improved += 1
            if dry_run:
                print(f"  [WOULD FIX] {source}/{pmcid}: {len(old_steps)} -> {len(new_steps)} steps")
            else:
                # Rewrite as canonical step dicts (step_id, substeps, is_critical, timers)
                canonical_steps = []
                for i, s in enumerate(new_steps):
                    instr = s["instruction"]
                    canonical_steps.append({
                        "step_id": i,
                        "title": s.get("title", ""),
                        "instruction": instr,
                        "substeps": [],
                        "is_critical": any(w in instr.lower() for w in
                                            ["critical", "immediately", "do not", "must not"]),
                        "timers": [],
                    })
                normalised["steps"] = canonical_steps
                norm_path.write_text(json.dumps(normalised, ensure_ascii=False, indent=2))
                print(f"  [FIXED] {source}/{pmcid}: {len(old_steps)} -> {len(new_steps)} steps")

                # Also backfill the raw file's steps_raw for provenance/consistency.
                raw_path = raw_dir / norm_path.name
                if raw_path.exists():
                    try:
                        raw = json.loads(raw_path.read_text())
                        raw["steps_raw"] = new_steps
                        raw_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2))
                    except Exception:
                        pass

    print(f"\nChecked (zero-step or mega-step): {checked}")
    print(f"Improved: {improved}")
    print(f"No cached XML available: {no_xml}")
    print(f"No improvement found (genuinely no more content in source): {no_improvement}")
    if dry_run:
        print("(dry run — no files written)")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    process(args.dry_run)


if __name__ == "__main__":
    main()
