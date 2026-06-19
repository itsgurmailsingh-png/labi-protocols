"""
Layer 2: Normalise raw source protocols to the canonical Labi schema.

Reads from:  data/sources/{source}/raw/*.json
Writes to:   data/sources/{source}/normalised/*.json

Uses Gemini Flash (free tier) for LLM normalisation.
Run per-source or all sources at once.

Usage:
    python scripts/normalise.py                   # all sources
    python scripts/normalise.py --source bio_protocol
    python scripts/normalise.py --source pubmed_central
"""

import json
import os
import re
import sys
import time
import pathlib
import argparse
import datetime

try:
    from groq import Groq as GroqClient
    GROQ_AVAILABLE = True
except ImportError:
    GROQ_AVAILABLE = False

try:
    from openai import OpenAI as OpenAIClient
    OPENAI_AVAILABLE = True
except ImportError:
    OPENAI_AVAILABLE = False

# ── Config ────────────────────────────────────────────────────────────────────
# Load .env file directly so keys are always available regardless of shell export method
_env_path = pathlib.Path(__file__).resolve().parent.parent / ".env"
if _env_path.exists():
    for _line in _env_path.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

GROQ_KEY     = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL   = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
OPENAI_KEY   = os.environ.get("OPENAI_API_KEY", "")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
SOURCES_DIR  = pathlib.Path("data/sources")
DELAY_SECS   = float(os.environ.get("NORMALISE_DELAY", "0.5"))
KNOWN_SOURCES = [
    "bio_protocol",
    "pubmed_central",
    "vendor",
    "protocols_io",
    "zenodo",           # CC-BY — Zenodo REST API, explicitly permits third-party tools
    "figshare",         # CC-BY — figshare public API, no prohibition found
    "openwetware",      # CC-BY-SA 3.0 — MediaWiki API, share-alike (not pure CC-BY)
    "github_opentrons", # Apache-2.0 / MIT — git clone of public repos, 832 protocols
    "star_protocols",          # CC-BY — STAR Protocols journal, PMC full-text XML
    "methodsx",                # CC-BY — MethodsX journal, PMC full-text XML
    "biological_procedures",   # CC-BY — Biological Procedures Online, Springer/PMC
    "current_protocols",       # CC-BY — Current Protocols OA subset, Wiley/PMC
]
RETRY_FALLBACKS = False  # set by --retry-fallbacks flag

CATEGORIES = [
    # Life Sciences — Molecular & Cell
    "Molecular Biology",
    "Genomics & Sequencing",
    "Cell Biology",
    "Biochemistry",
    "Protein Science",
    "Proteomics",
    "Metabolomics",
    "Immunology",
    "Virology",
    # Life Sciences — Organismal
    "Microbiology",
    "Neuroscience",
    "Plant Biology",
    "Animal Models",
    "Marine & Aquatic Biology",
    "Ecology",
    "Taxonomy & Biodiversity",
    "Environmental Science",
    "Soil Science & Agriculture",
    "Food Science",
    # Clinical & Applied
    "Histology & Pathology",
    "Clinical & Translational",
    "Epidemiology & Public Health",
    "Pharmacology & Toxicology",
    # Imaging & Structural
    "Imaging & Microscopy",
    "Structural Biology",
    # Computational & Cross-cutting
    "Bioinformatics",
    "Chemistry & Synthesis",
    "Materials Science",
    "Synthetic Biology",
    # Lab Technology & Methods
    "Lab Automation & Robotics",
    "Systematic Review & Meta-Analysis",
    # Catch-all — genuine last resort only
    "Other",
]

SYSTEM_PROMPT = f"""You are a lab protocol normaliser. Given minimal protocol metadata, return ONLY a JSON object with these fields:
{{
  "title": "Clean protocol title (max 120 chars)",
  "author": "First author or organisation name",
  "category": "One of: {', '.join(CATEGORIES)}",
  "tags": ["specific technique or topic", "organism or model system", "key reagent or instrument"],
  "estimated_time_mins": <integer, NEVER null — estimate from step count and types>,
  "materials": ["reagent or equipment", ...]
}}

Rules:
- estimated_time_mins: ALWAYS provide an integer. Use step_count and step_previews to estimate. Incubation/centrifuge steps = longer. If unsure: step_count * 6 as baseline.
- materials: reagents + equipment only, max 15 items. Use provided materials if available.
- category: pick the single BEST match. Use "Other" ONLY if nothing fits — with 27 categories available, almost everything should fit.
- tags: 3-6 free-text lowercase tags capturing the specific technique, organism, disease area, instrument, or application (e.g. "western blot", "mouse", "CRISPR", "diatoms", "soil pH", "LC-MS").
- Use plain text only, no markdown.
"""


# ── Helpers ───────────────────────────────────────────────────────────────────

def slugify(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_-]+", "_", text)
    return text[:80]


def strip_html(text: str) -> str:
    import html as _html
    text = _html.unescape(text or "")          # decode &amp; &lt; &nbsp; etc.
    text = re.sub(r"<[^>]+>", " ", text)       # strip HTML tags
    text = re.sub(r"\s{2,}", " ", text)        # collapse whitespace
    return text.strip()


_TIMER_RE = re.compile(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$")


def _parse_timer(text: str) -> int | None:
    """Parse HH:MM or HH:MM:SS → seconds. Returns None if not a timer."""
    m = _TIMER_RE.match(text.strip())
    if not m:
        return None
    h, mn, s = int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)
    return h * 3600 + mn * 60 + s


def _timer_label(secs: int) -> str:
    h, rem = divmod(secs, 3600)
    mn, s = divmod(rem, 60)
    if h and mn:
        return f"{h}h {mn}min"
    if h:
        return f"{h} hour{'s' if h > 1 else ''}"
    if mn and s:
        return f"{mn}min {s}s"
    if mn:
        return f"{mn} minute{'s' if mn > 1 else ''}"
    return f"{s} seconds"


def _split_text_into_steps(text: str) -> list:
    """
    Split a free-text description into logical steps.
    Handles: numbered lists (1. 2. 3.), paragraph breaks, or single block.
    """
    text = strip_html(text).strip()
    if not text:
        return []

    # Try numbered steps: "1.", "1)", "Step 1:", "Step 1."
    numbered = re.split(r"(?<!\w)(?:step\s+)?\d+[\.\)]\s+", text, flags=re.IGNORECASE)
    numbered = [s.strip() for s in numbered if len(s.strip()) > 20]
    if len(numbered) >= 3:
        return numbered

    # Try paragraph splits (double newline or single newline + capital letter)
    paras = re.split(r"\n{2,}|\n(?=[A-Z])", text)
    paras = [p.strip() for p in paras if len(p.strip()) > 20]
    if len(paras) >= 2:
        return paras

    # Single block — return as one step
    return [text] if len(text) > 20 else []


def _extract_substeps(instruction: str) -> list | None:
    """
    Detect and extract sub-steps from an instruction string.
    Returns a list of sub-step instruction strings, or None if no sub-steps found.
    Patterns detected:
      - a) text  b) text  c) text  (lettered, inline or newline)
      - 1a. text  1b. text  (alphanumeric sub-numbering)
      - bullet lines starting with -, •, –
      - roman numeral lines: i. ii. iii.
    """
    # Lettered sub-steps: a) ... b) ... c)  (at least 3)
    alpha_split = re.split(r"(?:^|\n)\s*[a-z]\)\s+", instruction, flags=re.MULTILINE)
    if len(alpha_split) >= 4:  # 4 = preamble + 3 sub-steps
        subs = [s.strip() for s in alpha_split if s.strip() and len(s.strip()) > 10]
        if len(subs) >= 3:
            return subs

    # Inline lettered: "... a) do this b) do that c) do other"
    inline_alpha = re.split(r"\s+[a-z]\)\s+", instruction)
    if len(inline_alpha) >= 3:
        subs = [s.strip() for s in inline_alpha if s.strip() and len(s.strip()) > 10]
        if len(subs) >= 3:
            return subs

    # Sub-numbered: 1a. 1b. 1c. (NOT decimal numbers like 1.5)
    subnumbered = re.split(r"(?:^|\n)\s*\d+[a-z][\.\)]\s+", instruction, flags=re.MULTILINE)
    if len(subnumbered) >= 4:
        subs = [s.strip() for s in subnumbered if s.strip() and len(s.strip()) > 10]
        if len(subs) >= 3:
            return subs

    # Bullet lines
    lines = instruction.split("\n")
    bullet_lines = [re.sub(r"^[\s•\-–\*]+", "", l).strip() for l in lines if re.match(r"^\s*[•\-–\*]\s+", l)]
    if len(bullet_lines) >= 3:
        return [b for b in bullet_lines if len(b) > 10]

    # Roman numerals: i. ii. iii. iv.
    roman_split = re.split(r"(?:^|\n)\s*(?:i{1,3}|iv|vi{0,3}|ix|x)[\.\)]\s+", instruction, flags=re.MULTILINE | re.IGNORECASE)
    if len(roman_split) >= 4:
        subs = [s.strip() for s in roman_split if s.strip() and len(s.strip()) > 10]
        if len(subs) >= 3:
            return subs

    return None


def steps_from_raw(steps_raw) -> list:
    """Convert raw steps (list OR string) to canonical step dicts with sub-step detection."""
    # If steps_raw is a plain string (e.g. Zenodo description), split it first
    if isinstance(steps_raw, str):
        raw_list = _split_text_into_steps(steps_raw)
    elif isinstance(steps_raw, list):
        raw_list = steps_raw
    else:
        return []

    result = []
    for i, step in enumerate(raw_list or []):
        if isinstance(step, dict):
            title = strip_html(step.get("title") or "")
            instruction = strip_html(step.get("instruction") or "")
        else:
            title = ""
            instruction = strip_html(str(step))
        instruction = instruction.strip()
        if not instruction:
            continue

        # Detect timer-only steps (e.g. "00:15:00") → convert to readable instruction
        timers = []
        timer_secs = _parse_timer(instruction)
        if timer_secs and timer_secs > 0:
            label = _timer_label(timer_secs)
            instruction = f"Wait {label}."
            if not title:
                title = f"Wait {label}"
            timers = [{"duration_secs": timer_secs, "label": label}]

        # Detect sub-steps within this instruction
        substeps = []
        if not timer_secs:
            raw_substeps = _extract_substeps(instruction)
            if raw_substeps and len(raw_substeps) >= 3:
                substeps = [{"instruction": s, "title": ""} for s in raw_substeps]

        result.append({
            "step_id": i,
            "title": title.strip(),
            "instruction": instruction,
            "substeps": substeps,
            "is_critical": any(w in instruction.lower() for w in ["critical", "immediately", "do not", "must not"]),
            "timers": timers,
        })
    return result


def _build_snippet(raw: dict) -> str:
    """Build minimal JSON snippet to send to LLM."""
    steps_raw = raw.get("steps_raw") or []
    step_titles = []
    for s in steps_raw[:15]:
        if isinstance(s, dict):
            t = s.get("title") or ""
            instr = s.get("instruction") or ""
            step_titles.append((t or instr[:80]).strip())
        else:
            step_titles.append(str(s)[:80])
    snippet = {
        "title": raw.get("title", ""),
        "abstract": (raw.get("abstract") or "")[:400],
        "author": raw.get("author", ""),
        "step_count": len(steps_raw),
        "step_previews": step_titles,
        "materials": (raw.get("materials") or [])[:10],
    }
    return json.dumps(snippet, ensure_ascii=False)


def _parse_llm_response(text: str) -> dict | None:
    """Parse LLM JSON response, stripping markdown fences and trailing text."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    # Extract just the JSON object — ignore anything after the closing brace
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        text = match.group(0)
    try:
        return json.loads(text)
    except Exception:
        return None


def llm_normalise(raw: dict) -> dict | None:
    """Call Groq (primary) → OpenAI (fallback). Never halts."""
    snippet = _build_snippet(raw)
    prompt = f"{SYSTEM_PROMPT}\n\nRaw data:\n{snippet}"

    # ── Groq (primary, free tier) ─────────────────────────────────────────────
    if GROQ_AVAILABLE and GROQ_KEY:
        try:
            client = GroqClient(api_key=GROQ_KEY)
            response = client.chat.completions.create(
                model=GROQ_MODEL,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=500,
                temperature=0.1,
            )
            result = _parse_llm_response(response.choices[0].message.content)
            if result:
                print("groq", end=" ")
                return result
            print(f"  Groq parse failed — trying OpenAI", end=" ")
        except Exception as e:
            print(f"  Groq API error: {e} — trying OpenAI", end=" ")

    # ── OpenAI (fallback) ─────────────────────────────────────────────────────
    if OPENAI_AVAILABLE and OPENAI_KEY:
        try:
            client = OpenAIClient(api_key=OPENAI_KEY)
            response = client.chat.completions.create(
                model=OPENAI_MODEL,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=500,
                temperature=0.1,
            )
            result = _parse_llm_response(response.choices[0].message.content)
            if result:
                print("openai", end=" ")
                return result
        except Exception as e:
            print(f"  OpenAI error: {e}", end=" ")

    return None


def _estimate_time(steps: list, steps_raw: list) -> int | None:
    """Estimate protocol time from step text — scans for time keywords."""
    import re as _re

    # Collect all instruction text
    texts = []
    for s in steps:
        texts.append(s.get("instruction", "") if isinstance(s, dict) else str(s))
    if not texts and steps_raw:
        for s in steps_raw:
            texts.append(s.get("instruction", "") if isinstance(s, dict) else str(s))

    full_text = " ".join(texts).lower()
    total_mins = 0

    # overnight / O/N = 720 mins (12h)
    overnight = len(_re.findall(r"\b(overnight|o/n)\b", full_text))
    total_mins += overnight * 720

    # explicit hours: "2 hours", "1.5 h", "2-3 hours" (take upper)
    for m in _re.finditer(r"(\d+(?:\.\d+)?)\s*(?:to|-)\s*(\d+(?:\.\d+)?)\s*h(?:ours?)?", full_text):
        total_mins += float(m.group(2)) * 60
    for m in _re.finditer(r"(\d+(?:\.\d+)?)\s*h(?:ours?)?\b", full_text):
        total_mins += float(m.group(1)) * 60

    # explicit minutes: "30 min", "45 minutes"
    for m in _re.finditer(r"(\d+)\s*(?:to|-)\s*(\d+)\s*min(?:utes?)?", full_text):
        total_mins += int(m.group(2))
    for m in _re.finditer(r"(\d+)\s*min(?:utes?)?\b", full_text):
        total_mins += int(m.group(1))

    # If we found real time signals, use them (cap at 7 days)
    if total_mins > 0:
        return min(int(total_mins), 10080)

    # Pure step-count fallback
    step_count = len(steps) or len(steps_raw)
    return max(step_count * 6, 5) if step_count else None


def build_canonical(raw: dict, llm: dict | None) -> dict:
    """Merge raw + LLM output into canonical Labi schema."""
    source_name = raw.get("source_name", "unknown")
    title = (llm or {}).get("title") or raw.get("title", "Untitled")

    if llm:
        steps = llm.get("steps") or steps_from_raw(raw.get("steps_raw"))
        category = llm.get("category") or "Other"
        tags = llm.get("tags") or []
        materials = llm.get("materials") or []
        estimated_time = llm.get("estimated_time_mins")
        author = llm.get("author") or raw.get("author", "")
    else:
        steps = steps_from_raw(raw.get("steps_raw"))
        category = raw.get("category") or "Other"
        tags = raw.get("keywords") or []
        materials = []
        estimated_time = None
        author = raw.get("author", "")

    # Fallback: estimate from step text if LLM returned null/0
    if not estimated_time:
        estimated_time = _estimate_time(steps, raw.get("steps_raw") or [])

    return {
        "protocol_id": slugify(title),
        "parent_protocol_id": None,
        "title": title,
        "author": author,
        "license": raw.get("license", "unknown"),
        "license_verified": raw.get("license_verified", False),
        "license_note": raw.get("license_note", ""),
        "source_name": source_name,
        "source_id": raw.get("source_id", ""),
        "source_url": raw.get("source_url", ""),
        "doi": raw.get("doi", ""),
        "citation": raw.get("doi", ""),
        "category": category,
        "tags": [t.lower().strip() for t in (tags or []) if t and len(t.strip()) > 1][:8],
        "estimated_time_mins": estimated_time,
        "materials": materials,
        "steps": steps,
        "verification_status": "verified" if raw.get("license_verified") else "unverified",
        "normalised_at": datetime.datetime.utcnow().isoformat() + "Z",
        "llm_normalised": llm is not None,
    }


# ── Main ──────────────────────────────────────────────────────────────────────

def process_source(source_name: str) -> tuple[int, int, int]:
    raw_dir = SOURCES_DIR / source_name / "raw"
    out_dir = SOURCES_DIR / source_name / "normalised"
    out_dir.mkdir(parents=True, exist_ok=True)

    if not raw_dir.exists():
        print(f"  Skipping {source_name}: no raw/ directory")
        return 0, 0, 0

    files = sorted(raw_dir.glob("*.json"))
    # In retry mode: only reprocess files that were normalised via fallback (llm_normalised=False)
    retry_stems = set()
    done = {f.stem for f in out_dir.glob("*.json")}

    fetched = skipped = errors = 0

    for path in files:
        if path.stem in done:
            # In retry mode, check if this was a fallback — if so reprocess
            out_path = out_dir / path.name
            try:
                existing = json.loads(out_path.read_text())
                if RETRY_FALLBACKS and not existing.get("llm_normalised", True):
                    pass  # fall through to reprocess
                else:
                    skipped += 1
                    continue
            except Exception:
                skipped += 1
                continue

        try:
            raw = json.loads(path.read_text())
        except Exception as e:
            print(f"  [ERROR] Read {path.name}: {e}")
            errors += 1
            continue

        if not raw.get("title"):
            print(f"  [SKIP] {path.name}: no title")
            skipped += 1
            continue

        print(f"  Normalising {path.name} ...", end=" ", flush=True)
        llm = llm_normalise(raw)
        if llm:
            print("LLM OK", end=" ")
        else:
            print("fallback", end=" ")

        canonical = build_canonical(raw, llm)

        # ── Sanity check before writing ───────────────────────────────────────
        steps = canonical.get("steps", [])
        if steps:
            first_instr = steps[0].get("instruction", "")
            if first_instr.startswith("{'title'") or first_instr.startswith('{"title"'):
                print(f"  [CORRUPT] steps serialised as string — skipping {path.name}")
                errors += 1
                continue
            if not first_instr.strip():
                print(f"  [WARN] empty instruction in step 0 — {path.name}")

        out_path = out_dir / path.name
        out_path.write_text(json.dumps(canonical, ensure_ascii=False, indent=2))
        print(f"→ {canonical['protocol_id'][:50]}")

        fetched += 1
        time.sleep(DELAY_SECS)

    return fetched, skipped, errors


def main():
    global RETRY_FALLBACKS
    parser = argparse.ArgumentParser(description="Normalise raw protocols to Labi schema")
    parser.add_argument("--source", choices=KNOWN_SOURCES, help="Process one source only")
    parser.add_argument("--retry-fallbacks", action="store_true", help="Re-run Gemini on fallback-only files")
    args = parser.parse_args()
    RETRY_FALLBACKS = args.retry_fallbacks

    print(f"DEBUG: GROQ_AVAILABLE={GROQ_AVAILABLE} GROQ_KEY={GROQ_KEY[:8] if GROQ_KEY else 'EMPTY'!r} OPENAI_KEY={OPENAI_KEY[:8] if OPENAI_KEY else 'EMPTY'!r}")
    print(f"LLM config: Groq={'YES ('+GROQ_MODEL+')' if GROQ_AVAILABLE and GROQ_KEY else 'NO'} | OpenAI={'YES' if OPENAI_AVAILABLE and OPENAI_KEY else 'NO'}")

    sources = [args.source] if args.source else KNOWN_SOURCES
    total_f = total_s = total_e = 0

    for source in sources:
        print(f"\n── {source} ──")
        f, s, e = process_source(source)
        total_f += f
        total_s += s
        total_e += e
        print(f"   {f} normalised | {s} skipped | {e} errors")

    print(f"\nDone: {total_f} total normalised, {total_s} skipped, {total_e} errors")


if __name__ == "__main__":
    main()
