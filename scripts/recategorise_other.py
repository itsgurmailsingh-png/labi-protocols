"""
Fast keyword-based re-categorisation of protocols stuck in 'Other'.
No LLM calls. Reads normalised files and updates category in place.

Usage:
    python scripts/recategorise_other.py
"""

import json, re, pathlib
from collections import Counter

SOURCES_DIR = pathlib.Path("data/sources")
KNOWN_SOURCES = [
    "bio_protocol", "pubmed_central", "vendor",
    "protocols_io", "zenodo", "figshare",
    "openwetware", "github_opentrons",
]

# keyword → category (checked in order, first match wins)
RULES = [
    # Lab Automation & Robotics
    ("Lab Automation & Robotics", [
        "automat", "liquid handling", "robotic", "opentrons", "tecan", "hamilton",
        "cherrypick", "cherry pick", "pipett", "labware", "ot-2", "ot2",
        "worklist", "scripting", "python protocol",
    ]),
    # Synthetic Biology
    ("Synthetic Biology", [
        "synthetic biology", "microfluidic", "organ-on-chip", "organ on chip",
        "3d print", "bioprint", "living material", "cyanobacteri", "biofabricat",
        "bioreactor design", "genetic circuit", "gene circuit", "chassis",
    ]),
    # Epidemiology & Public Health
    ("Epidemiology & Public Health", [
        "epidemiol", "public health", "covid", "sars-cov", "pandemic",
        "surveillance", "contact tracing", "vaccination protocol", "vaccine rollout",
        "outbreak", "prevalence study", "incidence study", "cohort study",
        "telerehabilitat", "community health",
    ]),
    # Systematic Review & Meta-Analysis
    ("Systematic Review & Meta-Analysis", [
        "systematic review", "meta-analysis", "scoping review", "literature review",
        "prisma", "data extraction protocol", "evidence synthesis",
        "umbrella review", "rapid review",
    ]),
    # Taxonomy & Biodiversity
    ("Taxonomy & Biodiversity", [
        "taxonom", "biodiversity", "species identif", "morphological identif",
        "specimen", "voucher", "herbarium", "phylogenetic identif",
        "dna barcod", "barcode identif", "entomolog", "insect collect",
        "microalgae identif", "algae classif",
    ]),
    # Bioinformatics (catch overflow)
    ("Bioinformatics", [
        "machine learning", "algorithm", "pipeline", "workflow automation",
        "data analysis pipeline", "hpc", "high-performance computing",
        "network analysis", "graph analysis",
    ]),
    # Imaging & Microscopy (catch overflow)
    ("Imaging & Microscopy", [
        "image analysis", "imagej", "fiji", "image segmentation",
        "fluorescence imaging", "confocal", "electron microscopy",
    ]),
]


def score(text: str, keywords: list[str]) -> int:
    t = text.lower()
    return sum(1 for kw in keywords if kw in t)


def reclassify(data: dict) -> str | None:
    """Return new category if confident, else None."""
    text = " ".join([
        data.get("title", ""),
        " ".join(data.get("tags", [])),
        " ".join(s.get("instruction", "") for s in data.get("steps", [])[:5]),
    ]).lower()

    best_cat = None
    best_score = 0
    for cat, keywords in RULES:
        s = score(text, keywords)
        if s > best_score:
            best_score = s
            best_cat = cat

    return best_cat if best_score >= 1 else None


def main():
    updated = skipped = 0
    new_cats = Counter()

    for src in KNOWN_SOURCES:
        norm_dir = SOURCES_DIR / src / "normalised"
        if not norm_dir.exists():
            continue
        for path in norm_dir.glob("*.json"):
            try:
                data = json.loads(path.read_text())
            except Exception:
                continue
            if data.get("category") != "Other":
                continue

            new_cat = reclassify(data)
            if new_cat:
                data["category"] = new_cat
                path.write_text(json.dumps(data, ensure_ascii=False, indent=2))
                new_cats[new_cat] += 1
                updated += 1
            else:
                skipped += 1

    print(f"\nRe-categorised {updated} protocols:")
    for cat, count in new_cats.most_common():
        print(f"  {count:4d}  {cat}")
    print(f"\n  {skipped} remain in Other")


if __name__ == "__main__":
    main()
