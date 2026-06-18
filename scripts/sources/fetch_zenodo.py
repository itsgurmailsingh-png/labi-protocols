"""
fetch_zenodo.py

Fetches CC-BY licensed lab protocols from Zenodo's public REST API.

Legal basis:
  - Zenodo metadata is CC0 — no restrictions on reuse
  - Zenodo Terms of Use: "Zenodo's simple web interface is supplemented by a rich
    API which allows third-party tools and services to use Zenodo as a backend
    in their workflow." (https://about.zenodo.org/)
  - No ToS clause restricts redistribution of CC-BY content fetched via API
  - License verified per-record from the Zenodo record metadata

Flow:
  1. Query Zenodo REST API with lab-specific keywords
  2. Filter: access_right=open, license field starts with cc-by (no nc/sa/nd)
  3. Fetch full record for description (used as steps_raw)
  4. Save to data/sources/zenodo/raw/{zenodo_id}.json
  5. Already-saved records are skipped (resume-safe)

Usage:
    python3 scripts/sources/fetch_zenodo.py
    MAX_RECORDS=500 python3 scripts/sources/fetch_zenodo.py
    ZENODO_TOKEN=your_token python3 scripts/sources/fetch_zenodo.py  # raises rate limit
"""

import json
import os
import re
import time
import pathlib
import datetime
import requests

# ── Config ────────────────────────────────────────────────────────────────────
TOKEN       = os.environ.get("ZENODO_TOKEN", "")
MAX_RECORDS = int(os.environ.get("MAX_RECORDS", "20000"))
PAGE_SIZE   = 25 if not TOKEN else 100   # unauthenticated: max 25
DELAY_SECS  = 0.4   # Zenodo public rate limit: ~60 req/min unauthenticated

RAW_DIR  = pathlib.Path("data/sources/zenodo/raw")
SKIP_LOG = pathlib.Path("data/sources/zenodo/skipped_license.jsonl")
BASE_URL = "https://zenodo.org/api/records"

HEADERS = {"User-Agent": "LabiApp/1.0 (mailto:support@getlabi.app)"}
if TOKEN:
    HEADERS["Authorization"] = f"Bearer {TOKEN}"

# Category-specific queries — "steps reagent procedure" forces relevance toward actual protocols
SEARCH_KEYWORDS = [
    # Molecular Biology
    "PCR protocol steps reagent procedure",
    "qPCR quantitative PCR protocol method steps",
    "DNA extraction isolation protocol procedure reagent",
    "RNA extraction isolation protocol steps reagent",
    "cloning restriction enzyme ligation protocol steps",
    "gel electrophoresis agarose protocol procedure",
    "bacterial transformation competent cells protocol steps",
    # Genomics & Sequencing
    "sequencing library preparation protocol steps reagent",
    "ChIP chromatin immunoprecipitation protocol procedure",
    "CRISPR Cas9 genome editing protocol steps reagent",
    "FISH fluorescence in situ hybridization protocol",
    # Cell Biology
    "cell culture protocol steps reagent procedure",
    "transfection protocol steps reagent procedure",
    "flow cytometry protocol staining procedure steps",
    "cell viability assay protocol procedure reagent",
    # Biochemistry & Protein Science
    "western blot protocol steps reagent procedure",
    "ELISA protocol steps reagent procedure",
    "protein purification chromatography protocol steps",
    "immunoprecipitation protocol steps reagent procedure",
    "protein expression purification protocol method",
    # Immunology & Imaging
    "immunofluorescence staining protocol steps procedure",
    "immunohistochemistry protocol steps reagent procedure",
    "confocal microscopy imaging protocol procedure steps",
    "FACS sorting protocol steps reagent procedure",
    # Microbiology
    "bacterial culture growth protocol steps reagent",
    "antimicrobial susceptibility protocol procedure method",
    "microbiome 16S sequencing protocol steps reagent",
    # Animal Models
    "mouse surgery dissection protocol steps procedure",
    "animal tissue processing protocol steps reagent",
    # Additional Molecular Biology
    "southern blot protocol procedure steps reagent",
    "northern blot RNA protocol procedure steps",
    "site directed mutagenesis protocol steps procedure",
    "in vitro transcription translation protocol steps",
    "EMSA electrophoretic mobility shift assay protocol",
    "co-immunoprecipitation coIP protocol steps reagent",
    "chromatin accessibility ATAC-seq protocol steps",
    "CUT&RUN CUT&TAG protocol steps procedure",
    # Additional Cell Biology
    "cell lysis protein extraction protocol steps",
    "cell fixation permeabilization protocol steps reagent",
    "mitochondria isolation fractionation protocol steps",
    "autophagy assay protocol steps procedure",
    "apoptosis caspase assay protocol steps",
    "colony formation assay protocol procedure steps",
    "scratch wound healing assay protocol steps",
    "calcium imaging protocol steps reagent procedure",
    # Additional Biochemistry
    "SDS-PAGE gel electrophoresis protein protocol steps",
    "Bradford protein assay protocol steps procedure",
    "BCA protein quantification assay protocol steps",
    "enzyme activity assay kinetics protocol steps",
    "mass spectrometry sample preparation protocol steps",
    "HPLC sample preparation protocol steps procedure",
    "spectrophotometry absorbance assay protocol steps",
    # Additional Immunology
    "ELISPOT assay protocol steps procedure reagent",
    "cytokine measurement ELISA protocol steps",
    "T cell activation stimulation protocol steps",
    "B cell culture differentiation protocol steps",
    "NK cell cytotoxicity assay protocol steps",
    "complement fixation assay protocol steps procedure",
    # Additional Genomics
    "whole genome sequencing library prep protocol steps",
    "RNA-seq library preparation protocol steps reagent",
    "single cell RNA sequencing scRNA-seq protocol steps",
    "bisulfite sequencing methylation protocol steps",
    "Hi-C chromosome conformation protocol steps",
    "exome capture sequencing protocol steps",
    "nanopore sequencing library protocol steps",
    # Additional Microbiology
    "yeast transformation protocol steps reagent procedure",
    "fungal culture isolation protocol steps procedure",
    "viral transduction lentiviral protocol steps",
    "phage display selection protocol steps procedure",
    "biofilm formation assay protocol steps",
    "minimum inhibitory concentration MIC protocol steps",
    # Histology & Pathology
    "tissue sectioning cryostat protocol steps procedure",
    "paraffin embedding tissue processing protocol steps",
    "H&E hematoxylin eosin staining protocol steps",
    "Masson trichrome staining protocol steps",
    "immunoperoxidase staining protocol steps procedure",
    # Neuroscience
    "brain slice electrophysiology patch clamp protocol",
    "stereotaxic surgery brain injection protocol steps",
    "neuron culture preparation protocol steps reagent",
    "behavioral test protocol mouse rat steps procedure",
    # Structural Biology
    "protein crystallization protocol steps procedure",
    "cryo-EM sample preparation protocol steps",
    "NMR sample preparation protein protocol steps",
    "surface plasmon resonance SPR assay protocol steps",
    # Plant Biology
    "plant transformation Agrobacterium protocol steps",
    "plant tissue culture regeneration protocol steps",
    "plant RNA extraction protocol steps reagent",
    "plant protein extraction immunoblot protocol steps",
    # Clinical & Translational
    "blood plasma serum isolation protocol steps",
    "urine sample processing protocol steps procedure",
    "PBMC isolation peripheral blood protocol steps",
    "fecal microbiome DNA extraction protocol steps",
    "tissue biopsy processing protocol steps procedure",
]

RAW_DIR.mkdir(parents=True, exist_ok=True)
SKIP_LOG.parent.mkdir(parents=True, exist_ok=True)


# ── License check ──────────────────────────────────────────────────────────────
def is_cc_by(license_id: str) -> bool:
    """Accept CC-BY and CC0. Reject NC/SA/ND variants."""
    lid = license_id.lower()
    if lid in ("cc-zero", "cc0-1.0", "cc0"):
        return True
    return (
        lid.startswith("cc-by")
        and "nc" not in lid
        and "sa" not in lid
        and "nd" not in lid
    )


def skip(record_id: str, reason: str, meta: dict):
    with open(SKIP_LOG, "a") as f:
        f.write(json.dumps({"id": record_id, "reason": reason,
                             "title": meta.get("title", ""), "ts": datetime.datetime.utcnow().isoformat()}) + "\n")


# ── Raw record → pipeline schema ──────────────────────────────────────────────
def to_raw_schema(record: dict) -> dict:
    meta = record.get("metadata", {})
    record_id = str(record.get("id", ""))
    license_id = meta.get("license", {}).get("id", "")
    creators = meta.get("creators", [])
    author = creators[0].get("name", "") if creators else ""
    description = meta.get("description", "") or ""
    # Strip HTML tags from description
    description_plain = re.sub(r"<[^>]+>", " ", description).strip()

    return {
        "source_name": "zenodo",
        "source_id": record_id,
        "source_url": f"https://zenodo.org/records/{record_id}",
        "doi": meta.get("doi", ""),
        "title": meta.get("title", ""),
        "author": author,
        "license": license_id,
        "license_verified": True,
        "license_note": f"Zenodo record metadata license field: {license_id}",
        "description": description_plain,
        "steps_raw": description_plain,   # normalise.py will structure this
        "keywords": meta.get("keywords", []),
        "resource_type": meta.get("resource_type", {}).get("type", ""),
        "fetched_at": datetime.datetime.utcnow().isoformat(),
    }


# ── Main fetch loop ────────────────────────────────────────────────────────────
def fetch_keyword(keyword: str, saved: set, total_saved: list) -> None:
    page = 1
    while True:
        if len(total_saved) >= MAX_RECORDS:
            return

        params = {
            "q": keyword,
            "size": PAGE_SIZE,
            "page": page,
            "sort": "bestmatch",
        }
        try:
            resp = requests.get(BASE_URL, params=params, headers=HEADERS, timeout=20)
            if resp.status_code == 429:
                print("  Rate limited — sleeping 60s")
                time.sleep(60)
                continue
            resp.raise_for_status()
        except requests.RequestException as e:
            print(f"  Request error for '{keyword}' page {page}: {e}")
            break

        data = resp.json()
        hits = data.get("hits", {}).get("hits", [])
        if not hits:
            break

        for record in hits:
            record_id = str(record.get("id", ""))
            if record_id in saved:
                continue

            meta = record.get("metadata", {})
            title = meta.get("title", "")
            license_id = meta.get("license", {}).get("id", "")

            if not license_id:
                skip(record_id, "no_license_field", meta)
                continue

            if not is_cc_by(license_id):
                skip(record_id, f"non_cc_by: {license_id}", meta)
                continue

            raw = to_raw_schema(record)
            out_path = RAW_DIR / f"{record_id}.json"
            out_path.write_text(json.dumps(raw, indent=2, ensure_ascii=False))
            saved.add(record_id)
            total_saved.append(record_id)
            print(f"  [{len(total_saved):>4}] {record_id}  {title[:70]}")

            if len(total_saved) >= MAX_RECORDS:
                return

        # Check if there's a next page
        if len(hits) < PAGE_SIZE:
            break
        page += 1
        time.sleep(DELAY_SECS)


def main():
    # Load already-saved IDs (resume-safe)
    saved = {p.stem for p in RAW_DIR.glob("*.json")}
    print(f"Resuming from {len(saved)} existing Zenodo records.")

    total_saved: list = []

    for keyword in SEARCH_KEYWORDS:
        if len(total_saved) >= MAX_RECORDS:
            break
        print(f"\nKeyword: '{keyword}'")
        fetch_keyword(keyword, saved, total_saved)
        time.sleep(DELAY_SECS)

    print(f"\nDone. New records saved: {len(total_saved)}")
    print(f"Total in raw dir:  {len(list(RAW_DIR.glob('*.json')))}")
    print(f"Skipped log:       {SKIP_LOG}")


if __name__ == "__main__":
    main()
