# labi-protocols

Data infrastructure for [Labi](https://github.com/itsgurmailsingh-png/labi) — an offline-first lab protocol assistant.

This repo runs a multi-source, license-verified pipeline that ingests raw protocol data, verifies every license via external registries, normalises to a canonical schema via LLM, recovers content from attached documents/media, restructures steps into bench-scannable titles + substeps, deduplicates across sources, gates everything through automated quality checks, and publishes to a CDN consumed by the Flutter app.

---

## Pipeline Overview

```
LAYER 1 — FETCH                    12 sources, license-gated at fetch time (CC-BY only)
    │
LAYER 2 — NORMALISE                raw JSON → canonical schema via LLM (title/category/materials/steps)
    │
LAYER 2.5 — RECOVER                 pull content out of attached PDF/DOCX docs and PMC figures for
    │                               protocols that came through with zero usable steps
    │
LAYER 2.6 — RESTRUCTURE             LLM pass gives every step a real title, breaks long steps into
    │                               substeps — verified against a content-preservation check per step
    │
LAYER 3 — MERGE + DEDUP             MinHash LSH across sources, zero-step protocols withheld from publish
    │
LAYER 3.5 — QUALITY GATE            verify_protocol_quality.py + final_validation.py — structural
    │                               integrity checks that must pass before layer 4 runs
    │
LAYER 4 — INDEX                     search_index.json (Flutter asset) + index.json (website)
    │
DELIVERY                            jsDelivr CDN ← this GitHub repo
```

Each layer is a separate script under `scripts/`, chainable via `scripts/pipeline.py`. Layers 2.5/2.6/3.5 are newer additions (see "Data Quality" below for why they exist).

---

## Directory Structure

```
labi-protocols/
│
├── protocols/                          ← PUBLIC: CDN-served protocol JSONs (zero-step protocols withheld)
│   └── {protocol_id}.json
│
├── search_index.json                   ← PUBLIC: compact metadata index (Flutter asset)
├── index.json                          ← PUBLIC: website-facing index (different key set)
│
├── data/
│   ├── archive/
│   │   └── protocols_io_unverified/    ← OLD data, license unconfirmed, NOT distributed
│   │
│   ├── raw/                            ← untouched API/document caches, used for offline re-processing
│   │   ├── protocols_io/               ← full protocols.io API responses (6,101 files)
│   │   └── pmc/                        ← full JATS XML for every PMC-family article (4,809 files)
│   │
│   ├── media/                          ← downloaded images/PDFs/DOCX referenced by protocols
│   │   ├── zenodo/{record_id}/
│   │   ├── {pmc_source}/{pmcid}/       ← star_protocols, methodsx, biological_procedures, etc.
│   │   └── protocols_io_docs/{slug}/   ← recovered document attachments
│   │
│   ├── sources/
│   │   └── {source_name}/
│   │       ├── raw/                    ← license-verified, source-native JSON
│   │       ├── normalised/             ← Labi schema, LLM-normalised
│   │       └── skipped_license.jsonl   ← protocols that failed license check
│   │
│   └── merged/                         ← post-dedup merged protocols (includes withheld zero-step ones,
│                                          for traceability — protocols/ is the filtered public subset)
│
├── scripts/
│   ├── pipeline.py                     ← orchestrator: layers 1-4, includes the quality gate (layer 3b)
│   ├── normalise.py                    ← Layer 2: raw → Labi schema via LLM
│   ├── merge_and_dedup.py              ← Layer 3: MinHash LSH dedup, withholds zero-step protocols
│   ├── verify_protocol_quality.py      ← Layer 3.5: structural integrity gate (exit 1 on failure)
│   ├── final_validation.py             ← last-mile check on the published artifact itself
│   ├── restructure_steps_llm.py        ← Layer 2.6: title + substep generation via LLM
│   ├── test_step_parsing.py            ← unit tests for the parsing functions themselves
│   │
│   ├── fetch_protocols_io_documents.py ← recovers steps from protocols.io's attached PDF/DOCX docs
│   ├── extract_steps_from_media_pdf.py ← general-purpose PDF/DOCX text recovery, any source
│   ├── backfill_pmc_family_steps.py    ← re-extracts steps from cached PMC XML (handles nested
│   │                                      sections and body-level paragraphs outside <sec>)
│   ├── backfill_protocols_io_steps.py  ← re-extracts steps from cached protocols.io API responses
│   ├── detect_genuine_protocol.py      ← flags likely-not-a-protocol content (informational only,
│   │                                      NOT wired into publish filtering — see Data Quality below)
│   ├── fix_broken_steps.py             ← background/materials reclassification (safe part only —
│   │                                      the regex-based step splitter is permanently disabled)
│   ├── fix_percent_s_corruption.py     ← strips leftover %s template artifacts from protocols.io's
│   │                                      own broken table-rendering API responses
│   ├── revert_steps_to_raw.py          ← regenerates `steps` straight from raw, undoing any bad
│   │                                      processing pass — the emergency-repair tool
│   ├── recategorise_other.py           ← keyword-based reclassification out of the "Other" category
│   ├── build_index.py                  ← builds index.json (website format)
│   │
│   └── sources/
│       ├── fetch_protocols_io_verified.py  ← protocols.io + CrossRef gate — FROZEN, do not re-run
│       ├── fetch_pubmed_central.py          ← PMC E-utilities + XML license gate
│       ├── fetch_zenodo.py                  ← Zenodo REST API
│       ├── fetch_zenodo_media.py            ← downloads image/PDF attachments for Zenodo records
│       ├── fetch_figshare.py                ← figshare API (POST /articles/search, not GET)
│       ├── fetch_openwetware.py             ← MediaWiki API — currently TLS-unreachable, unrelated to code
│       ├── fetch_bio_protocol.py            ← WAF-blocked, unresolved
│       └── fetch_pmc_figures.py             ← downloads figures for all PMC-family sources
│
├── requirements.txt
└── README.md
```

---

## Sources (12 total)

| Source | Raw fetched | Normalised | Status |
|---|---|---|---|
| protocols.io | 6,291 | 6,291 | ✅ Complete — **frozen, do not re-fetch new protocols** |
| Zenodo | 20,493 | 2,269 (11%) | ⏸️ **Backlog paused — 18,224 records unprocessed, revisit later** |
| MethodsX | 2,658 | 2,658 | ✅ Complete — via PMC (real peer-reviewed methods journal, not related to Zenodo) |
| STAR Protocols | 1,076 | 1,076 | ✅ Complete — via PMC |
| bio-protocol | 1,114 | 435 | 🟡 Partial |
| github_opentrons | 757 | 832 | ✅ Complete |
| PubMed Central (general) | 280 | 53 | 🟡 Partial |
| Biological Procedures Online | 247 | 247 | ✅ Complete — via PMC |
| figshare | 213 | 213 | ✅ Complete (rate-limited by anti-abuse without an API token — could expand with one) |
| Current Protocols | 113 | 113 | ✅ Complete — via PMC's open subset, NOT the paywalled Wiley platform |
| OpenWetWare | 0 | 0 | ❌ TLS-unreachable from this network (not a code issue) |
| bio-protocol.org (direct) | 0 | 0 | ❌ WAF-blocked (HTTP 468) |
| vendor | 0 (archived) | 0 | ❌ Archived — old unverified data, not distributed |

**Sources evaluated and rejected** (checked their ToS/license directly, not assumed): Cold Spring Harbor Protocols (subscription + explicit ban on automated access), Addgene (ToS bans automated scraping + redistribution), Abcam (personal-use-only, no redistribution), Springer Protocols (subscription database), JoVE (institutional-login paywalled), Nature Protocols the journal (subscription — distinct from Protocol Exchange below, which is open).

### ⏳ Sources identified, confirmed usable, PENDING import (zero protocols fetched yet)

| Source | License | Access | Notes |
|---|---|---|---|
| **The OLB (Open Lab Book)** | CC BY-SA 2.5 | GitHub-hosted (`mfitzp`), readthedocs | Share-alike — rank below pure CC-BY sources in dedup, same as OpenWetWare |
| **Protocol Exchange** (Nature, via Research Square) | CC-BY 4.0, DOI-assigned | `protocolexchange.researchsquare.com` — direct fetch 403s (bot protection), need to request an official JSON API key at `researchsquare.com/request-api` first | Separate from the paywalled Nature Protocols journal — this one is genuinely open |

No fetchers built yet for either. See tasks #25/#26.

---

## Protocol Schema (current)

```jsonc
{
  "protocol_id": "trizol_rna_extraction_from_cultured_cells",
  "parent_protocol_id": null,
  "version_count": 1,
  "title": "TRIzol RNA Extraction from Cultured Cells",
  "author": "Thermo Fisher Scientific",

  "license": "CC-BY 4.0",
  "license_verified": true,
  "license_url": "https://creativecommons.org/licenses/by/4.0/",
  "license_note": "Confirmed via CrossRef DOI metadata: 10.17504/protocols.io.xxxxx",

  "source_name": "protocols_io",
  "source_url": "https://www.protocols.io/view/...",
  "doi": "10.17504/protocols.io.xxxxx",
  "citation": "",
  "peer_reviewed": false,
  "stats": { "views": 0, "runs": 0, "bookmarks": 0, "comments": 0 },
  "quality_score": 12.5,

  "verification_status": "verified",
  "category": "Molecular Biology",
  "estimated_time_mins": 90,

  "materials": ["TRIzol Reagent", "chloroform", "isopropanol"],

  "steps": [
    {
      "step_id": 0,
      "title": "Lyse Cells",
      "instruction": "Add 1 mL TRIzol directly to the well...",
      "substeps": [
        { "title": "", "instruction": "Add 1 mL TRIzol" },
        { "title": "", "instruction": "Incubate 5 minutes at room temperature" }
      ],
      "is_critical": false,
      "timers": []
    }
  ],

  // Media — images/PDFs/DOCX downloaded and locally hosted (see Data Quality: 11% gap remains)
  "media": [
    {
      "type": "image",  // "image" | "pdf" | "document"
      "filename": "fig2_western_blot.png",
      "local_path": "data/media/zenodo/16731878/fig2_western_blot.png",
      "source_url": "https://zenodo.org/api/records/16731878/files/fig2_western_blot.png",
      "caption": "",
      "bytes": 482113
    }
  ],

  // Informational only — NOT used to filter what publishes (see Data Quality)
  "content_type": "protocol",       // "protocol" | "likely_not_protocol"
  "content_type_score": 66,

  "merged_at": "2026-07-09"
}
```

---

## LLM Configuration

Normalisation (title/category/materials) and step restructuring (titles + substeps) both call out to an LLM, in this fallback order:

1. **Ollama Cloud** (`OLLAMA_API_KEY`, model `gpt-oss:120b-cloud` by default) — primary for restructuring
2. **Groq** (`GROQ_API_KEY`) — fast, free-tier, rate-limits easily under sustained load
3. **OpenAI** (`OPENAI_API_KEY`, `gpt-4o-mini`) — fallback

```bash
export OLLAMA_API_KEY="..."      # ollama.com
export OLLAMA_MODEL="gpt-oss:120b-cloud"   # default; gpt-oss:20b-cloud for throughput over quality
export GROQ_API_KEY="..."
export OPENAI_API_KEY="..."
```

All LLM-touching scripts have a **hard content-preservation safety check**: every number/quantity/reagent name in the original text must survive verbatim in the restructured output, or the restructuring is rejected and the step is left untouched. See `scripts/restructure_steps_llm.py` for the implementation — this exists because a regex-based (non-LLM) attempt at the same problem shredded real content in production (see Data Quality below).

---

## Data Quality — read before trusting a number in this repo

This pipeline has been through a serious quality audit. Real bugs were found and fixed; some things are known-open. Documenting both honestly:

### Fixed (permanent regression tests exist for all of these — `scripts/test_step_parsing.py`)

- **Eaten-marker regex corruption**: a numbered-list splitter matched digit+punctuation patterns *anywhere* in a sentence (not just real step boundaries), silently deleting the matched text. Shredded sentences like "...tools (eg, PROBAST) [25]; and (**6**) narrative synthesis..." into scrambled fragments. Disabled permanently in `fix_broken_steps.py` and `normalise.py`; 11,531+ files reverted to clean text.
- **`%s %s` template leakage**: protocols.io's own API serves malformed HTML for table-type step components (primer/oligo tables) — confirmed against the untouched cached API response, not our bug, not recoverable, but no longer displayed. `scripts/fix_percent_s_corruption.py`.
- **Zenodo catalog-number-eating bug**: the same class of regex-eats-real-numbers bug, found independently in `normalise.py`'s free-text splitter — was deleting real content (museum catalog numbers, specimen IDs) that happened to look like "digit. " step markers. Affected 358 zenodo protocols, fixed.
- **PMC body-level content**: some MethodsX-style articles put real content as `<p>` directly under `<body>`, never wrapped in a `<sec>` — the section-walking extractor missed these entirely. Fixed in `backfill_pmc_family_steps.py`.
- **Zero-step protocols with recoverable attachments**: ~400+ protocols_io protocols had zero steps not because the content didn't exist, but because the author put the real procedure in an attached PDF/DOCX that was never fetched. Recovery pipeline built (`fetch_protocols_io_documents.py`, `extract_steps_from_media_pdf.py`).

### Known, open, unresolved

- **11% of published protocols (1,488 of 13,318) reference a figure/table/file in their text with no matching media attached.** Real gap, not yet closed — media fetching is still in progress for several sources.
- **~4,300 protocols flagged `content_type: likely_not_protocol`** (academic papers/reviews that got swept in, not real procedures) — **deliberately not excluded from publish**. Every exclusion rule tried (materials presence, step-count + score combined) produced real false positives on legitimate protocols (a genuine mouse anesthesia protocol, a real Patch-Seq neuroscience protocol, "Morris Water Maze" — a standard behavioral assay). The tag is informational only; do not build an auto-filter on it without a materially better signal.
- **Materials/equipment lists sometimes get miscategorized as procedure steps**, inflating step counts and diluting quality signals (found via the Patch-Seq false-positive investigation) — not yet systematically fixed.
- **1,561 protocols have a single unsplit "mega-step" (>800 chars)** — content is complete and correct, just not broken into individual actions. Deliberate tradeoff: the regex splitter that used to reduce this count was also the one causing the eaten-marker corruption above.
- **2,013 protocols still categorized "Other"** — `recategorise_other.py`'s keyword rules only catch some of the backlog.
- **Scientific/factual accuracy of protocol content is out of scope for this pipeline** — verified structural integrity (the JSON isn't corrupted, numbers/text survive processing intact), not whether the underlying procedure is itself correct. That's the original source's responsibility.

Run `python3 scripts/verify_protocol_quality.py` (structural gate, exit code meaningful) and `python3 scripts/final_validation.py` (schema + numeric well-formedness + sentence coherence) before trusting any given snapshot of `protocols/`.

---

## License Verification System

Every protocol has a **hard-verified license**, checked at fetch time, never assumed.

| Source | Verification method | Authority |
|---|---|---|
| protocols.io | CrossRef DOI API → `license[0].URL` | DOI registry |
| PubMed Central (+ MethodsX, STAR Protocols, Biological Procedures, Current Protocols) | PMC article XML → `<ali:license_ref>` | Publisher deposit at submission |
| Zenodo | Record metadata `license` field | Zenodo, CC0 metadata / CC-BY content |
| figshare | Article detail endpoint `license.name` | figshare |
| bio-protocol.org | Terms of Service | ToS states all content CC-BY 4.0 |
| OpenWetWare | Terms of Use | **CC-BY-SA 3.0** — share-alike, ranked lowest in dedup priority |

```python
def is_cc_by(url):
    return (
        "creativecommons.org/licenses/by" in url
        and "/nc" not in url and "/sa" not in url and "/nd" not in url
    )
```

Protocols failing this check are never saved — logged to `skipped_license.jsonl` only.

---

## How to Run

```bash
pip install -r requirements.txt
```

```bash
# Full pipeline: fetch (only for sources not frozen) → normalise → merge → gate → index
python3 scripts/pipeline.py --layer 2,3,4

# Individual layers
python3 scripts/normalise.py --source zenodo
python3 scripts/restructure_steps_llm.py --source protocols_io
python3 scripts/merge_and_dedup.py
python3 scripts/verify_protocol_quality.py
python3 scripts/final_validation.py
python3 scripts/pipeline.py --layer 4
```

⚠️ **protocols.io is frozen** — do not run `fetch_protocols_io_verified.py` for new protocols.
⚠️ **Zenodo backlog (18,224 unprocessed records) is intentionally paused** — do not resume without explicit confirmation.

---

## CDN Delivery

```
https://cdn.jsdelivr.net/gh/itsgurmailsingh-png/labi-protocols@main/protocols/{protocol_id}.json
```

```bash
git add protocols/ search_index.json index.json
git commit -m "data: ..."
git push origin main
```

Cache purge (immediate): `https://purge.jsdelivr.net/gh/itsgurmailsingh-png/labi-protocols@main/search_index.json`

**Nothing in this repo has been pushed since 2026-06-19** — all pipeline work described above is local, uncommitted, and not yet deployed to the CDN or the live app.

---

## Flutter Integration

| File | How used |
|---|---|
| `search_index.json` | Bundled Flutter asset, loaded at startup for instant offline search |
| `protocols/*.json` | Lazy-fetched from CDN on open, cached in Isar |

---

## License

**Protocol data:** CC-BY 4.0 (or CC-BY-SA 3.0 for OpenWetWare-sourced entries) — attribution required, `source_url` provides the canonical link.

**Pipeline code (`scripts/`):** MIT.
