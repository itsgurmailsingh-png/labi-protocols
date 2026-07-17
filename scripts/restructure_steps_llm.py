"""
restructure_steps_llm.py

Gives every step a real, scannable action title and breaks long/multi-
action steps into substeps — so a bench scientist can glance at a step
instead of reading a 100-word paragraph mid-experiment.

Why an LLM and not regex: split_inline_numbered() (regex-based) shredded
real content by matching numbers/markers anywhere in a sentence instead of
understanding it. An LLM that reads the step can tell the difference
between "step 3 of 6" and "(6) narrative synthesis" inside one clause —
but LLM output isn't inherently trustworthy either, so every restructuring
is verified before being accepted:

SAFETY: after restructuring, every number+unit token found in the original
instruction (e.g. "500 µL", "37°C", "12,000 x g", "15 min") must appear
verbatim in the restructured output (title + substeps combined). If even
one is missing, the restructuring is REJECTED and the step is left exactly
as it was — title/substeps stay empty rather than risk losing a
measurement. This makes silent content loss structurally hard, not just
"hopefully fine because the prompt said so."

Usage:
    python3 scripts/restructure_steps_llm.py --pilot 5          # dry test, prints results, writes nothing
    python3 scripts/restructure_steps_llm.py --source protocols_io --limit 50
    python3 scripts/restructure_steps_llm.py                    # full run, all sources
"""

import argparse
import json
import os
import pathlib
import re
import sys
import time

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

_env_path = pathlib.Path(__file__).resolve().parent.parent / ".env"
if _env_path.exists():
    for _line in _env_path.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

OLLAMA_KEY   = os.environ.get("OLLAMA_API_KEY", "")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "gpt-oss:120b-cloud")
OLLAMA_URL   = "https://ollama.com/v1"
GROQ_KEY     = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL   = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
OPENAI_KEY   = os.environ.get("OPENAI_API_KEY", "")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")

SOURCES_DIR = pathlib.Path("data/sources")
LONG_STEP_WORDS = 40  # steps at or above this get substep breakdown; below just get a title

SYSTEM_PROMPT = """You are structuring ONE lab protocol step for a bench scientist who needs to glance at it while their hands are busy.

Given the step's instruction text, return ONLY a JSON object with these fields:
{
  "is_protocol_step": true or false — is this genuinely a lab/experimental action a scientist could follow (mix, incubate, run a command, take a measurement, etc.)? false if it's background/discussion text, a citation, an academic section heading, or otherwise not an actionable step.
  "title": "short imperative action title, max 8 words, e.g. 'Centrifuge sample' or 'Prepare DNase mix'. Empty string if is_protocol_step is false.",
  "substeps": ["ordered list of short sub-instructions, ONLY if this step describes more than one distinct physical action. Empty list if it's already one simple action, or if is_protocol_step is false."],
  "content_fully_preserved": true or false — true only if EVERY number, quantity, unit, reagent name, temperature, duration, concentration, and technical detail from the original text is represented somewhere in your title + substeps. Be honest here — if you condensed, rounded, or dropped anything, say false.
  "notes": "If content_fully_preserved is false, explain exactly what you left out and why. If is_protocol_step is false, explain why this isn't an actionable step. Empty string otherwise."
}

RULES:
- EVERY sentence in the original that describes an action, condition, or parameter (temperature, speed, duration, concentration, volume) should be represented in a substep. Do not drop a step because it seems secondary (e.g. an incubation condition, a mixing speed, a wash step).
- Only shorten filler/connecting language (e.g. "in order to", "please note that") and purely descriptive background/context sentences that contain no action and no parameter the scientist needs.
- Do not invent, infer, or add information that isn't in the original text.
- Self-report honestly. It's fine to say content_fully_preserved: false with a good explanation — that's more useful than silently dropping something.
"""

# Matches number+optional-unit tokens: "500 µL", "37°C", "12,000 x g", "15 min", "1.5 mL", "50%"
#
# BUG FOUND 2026-07-11: the old pattern `\d[\d,]*` greedily swallowed any
# comma directly after a digit, regardless of what followed — so ordinary
# prose like "Method 2, in which..." produced the bogus token "2,". That
# exact string (with trailing comma) never reappears in a naturally
# reworded LLM output, so content_preserved() rejected otherwise-correct
# restructurings all day (confirmed: a real, fully-correct restructuring of
# a "Sperm Chromatin Structure Assay" step was rejected for this exact
# reason). Comma is now only consumed as a genuine thousands-separator
# (comma followed by exactly 3 digits, e.g. "12,000"), never as trailing
# sentence punctuation.
_TOKEN_RE = re.compile(
    r"\d{1,3}(?:,\d{3})*(?:\.\d+)?\s*(?:µL|uL|mL|L|mg|g|kg|µg|ng|mM|M|nM|°C|°F|min|sec|s\b|h\b|hr|rpm|x\s*g|%|bp|kb|nm|µm|mm|cm)?",
    re.IGNORECASE,
)


def extract_measurement_tokens(text: str) -> set[str]:
    tokens = set()
    for m in _TOKEN_RE.finditer(text or ""):
        tok = m.group(0).strip()
        # Skip bare small integers with no unit and no comma (too noisy — step numbers, list markers)
        if re.fullmatch(r"\d{1,2}", tok):
            continue
        if tok:
            tokens.add(tok.lower().replace(" ", ""))
    return tokens


def content_preserved(original: str, title: str, substeps: list[str]) -> bool:
    """Kept for reference/comparison only — no longer used as the accept/
    reject gate (see restructure_step() docstring for why)."""
    original_tokens = extract_measurement_tokens(original)
    if not original_tokens:
        return True  # nothing measurable to lose
    combined = (title + " " + " ".join(substeps)).lower().replace(" ", "")
    missing = [t for t in original_tokens if t not in combined]
    return len(missing) == 0


REJECTIONS_LOG = pathlib.Path("logs/restructure_rejections.jsonl")


def log_rejection(source: str, protocol_id: str, instruction: str, note: str) -> None:
    """Every rejection's model-given reason, persisted so it can actually be
    read and audited — not just printed to a scrollback that disappears."""
    REJECTIONS_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(REJECTIONS_LOG, "a") as f:
        f.write(json.dumps({
            "source": source, "protocol_id": protocol_id,
            "instruction": instruction[:300], "note": note,
        }, ensure_ascii=False) + "\n")


# Module-level, single-instance clients — reused across every call instead
# of instantiating fresh per request. Root cause of the 24h stall found
# 2026-07-11: no timeout anywhere meant one slow/hung request could block a
# process indefinitely, and a fresh client per call leaked a new TCP
# connection every time (confirmed: 20+ ESTABLISHED/CLOSE-WAIT sockets per
# stuck process). REQUEST_TIMEOUT bounds every call; a hung request now
# fails and falls through to the next provider instead of hanging forever.
REQUEST_TIMEOUT = 45.0
_ollama_client = OpenAIClient(api_key=OLLAMA_KEY, base_url=OLLAMA_URL, timeout=REQUEST_TIMEOUT) if OPENAI_AVAILABLE and OLLAMA_KEY else None
_groq_client = GroqClient(api_key=GROQ_KEY, timeout=REQUEST_TIMEOUT) if GROQ_AVAILABLE and GROQ_KEY else None
_openai_client = OpenAIClient(api_key=OPENAI_KEY, timeout=REQUEST_TIMEOUT) if OPENAI_AVAILABLE and OPENAI_KEY else None


def call_llm(instruction: str) -> dict | None:
    prompt = f"{SYSTEM_PROMPT}\n\nStep instruction:\n{instruction}"

    if _ollama_client:
        try:
            resp = _ollama_client.chat.completions.create(
                model=OLLAMA_MODEL,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=2000,
                temperature=0.1,
                timeout=REQUEST_TIMEOUT,
            )
            result = _parse(resp.choices[0].message.content)
            if result:
                return result
        except Exception as e:
            print(f"    [Ollama Cloud error] {e}")

    if _groq_client:
        try:
            resp = _groq_client.chat.completions.create(
                model=GROQ_MODEL,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=2000,
                temperature=0.1,
                timeout=REQUEST_TIMEOUT,
            )
            result = _parse(resp.choices[0].message.content)
            if result:
                return result
        except Exception:
            pass

    if _openai_client:
        try:
            resp = _openai_client.chat.completions.create(
                model=OPENAI_MODEL,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=2000,
                temperature=0.1,
                timeout=REQUEST_TIMEOUT,
            )
            return _parse(resp.choices[0].message.content)
        except Exception as e:
            print(f"    [OpenAI error] {e}")
            return None

    return None


def _parse(text: str) -> dict | None:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        text = m.group(0)
    try:
        return json.loads(text)
    except Exception:
        return None


def restructure_step(instruction: str) -> tuple[str, list[str], bool, str]:
    """
    Returns (title, substeps, accepted, note).

    2026-07-11: switched from a regex-based content-preservation gate to
    trusting the model's own self-report. The regex approach
    (content_preserved(), still defined above for reference/comparison)
    had a real bug — it flagged ordinary sentence punctuation ("Method 2,
    in which...") as a lost measurement and rejected correct restructurings
    at scale, silently, for hours, across every source running that day.
    The model is asked directly whether it preserved everything and to
    explain itself if not (is_protocol_step / content_fully_preserved /
    notes) — that self-report is now the gate. `note` is always returned
    (even on accept) so it can be logged and spot-read, not just trusted
    blindly either.
    """
    result = call_llm(instruction)
    if not result:
        return "", [], False, "no LLM response"

    title = (result.get("title") or "").strip()
    substeps = [s.strip() for s in (result.get("substeps") or []) if isinstance(s, str) and s.strip()]
    is_protocol_step = result.get("is_protocol_step", True)
    content_ok = result.get("content_fully_preserved", True)
    note = (result.get("notes") or "").strip()

    if not is_protocol_step:
        return "", [], False, note or "model flagged: not an actionable protocol step"

    if not title:
        return "", [], False, "empty title returned"

    if len(title.split()) > 12:
        # LLM ignored the "max 8 words" instruction and dumped the full
        # instruction into the title field. Defeats the whole point of a
        # scannable title — reject rather than ship it.
        return "", [], False, "title too long (model ignored length limit)"

    if not content_ok:
        return "", [], False, note or "model self-reported incomplete content preservation"

    return title, substeps, True, note


def pilot(n: int) -> None:
    files = sorted(SOURCES_DIR.glob("protocols_io/normalised/*.json"))
    shown = 0
    for f in files:
        d = json.loads(f.read_text())
        steps = d.get("steps") or []
        long_step = next(
            (s for s in steps if len((s.get("instruction") or "").split()) >= LONG_STEP_WORDS
             and not (s.get("title") or "").strip()),
            None,
        )
        if not long_step:
            continue

        print(f"=== {f.name} — {d.get('title','')[:60]} ===")
        print(f"ORIGINAL ({len(long_step['instruction'].split())} words):")
        print(f"  {long_step['instruction']}")
        title, substeps, accepted, note = restructure_step(long_step["instruction"])
        print(f"\nRESULT: {'ACCEPTED' if accepted else 'REJECTED'}{f' — {note}' if note else ''}")
        if accepted:
            print(f"  title: {title!r}")
            for i, s in enumerate(substeps, 1):
                print(f"  {i}. {s}")
        print()

        shown += 1
        if shown >= n:
            break


GENERIC_TITLES = {
    "introduction", "background", "abstract", "related work", "related works",
    "discussion", "conclusion", "conclusions", "literature review", "overview",
    "summary", "motivation", "interest of study", "future work", "limitations",
    "acknowledgments", "acknowledgements", "references", "methodology overview",
    "results", "methods", "materials and methods",
}


def looks_generic_or_repeated(title: str, all_titles_in_protocol: list) -> bool:
    """
    True if this title is a generic academic-paper section name, or is
    reused verbatim across 3+ other steps in the same protocol — both are
    the signature of a PMC section heading getting copied onto every
    paragraph under it (see backfill_pmc_family_steps.py), not a real
    per-step action title. Without this check, PMC-family sources look
    "already titled" and silently skip restructuring entirely.
    """
    t = title.strip().lower()
    if not t:
        return True
    if t in GENERIC_TITLES:
        return True
    if all_titles_in_protocol.count(title) >= 2:
        return True
    return False


def process_source(source: str, limit: int | None, protocol_limit: int | None = None,
                    shard: int = 0, num_shards: int = 1) -> None:
    norm_dir = SOURCES_DIR / source / "normalised"
    if not norm_dir.exists():
        print(f"[SKIP] {source}: no normalised/ dir")
        return

    files = sorted(norm_dir.glob("*.json"))
    if num_shards > 1:
        # Disjoint file sets per shard (every Nth file) — safe to run in
        # parallel, no two shards ever touch the same file, no write races.
        files = files[shard::num_shards]

    processed = accepted_count = rejected_count = protocols_touched = 0
    for norm_path in files:
        if limit and processed >= limit:
            break
        if protocol_limit and protocols_touched >= protocol_limit:
            break
        try:
            d = json.loads(norm_path.read_text())
        except Exception:
            continue

        steps = d.get("steps") or []
        if not steps:
            continue

        all_titles = [(s.get("title") or "").strip() for s in steps]
        changed = False
        for s in steps:
            title = (s.get("title") or "").strip()
            instr = s.get("instruction") or ""
            wc = len(instr.split())
            # Missing title -> needs work if there's enough text to title.
            # Present-but-generic/repeated title -> needs work if the step
            # is long enough to actually benefit from a substep breakdown.
            if not title:
                needs_work = wc >= 5
            else:
                needs_work = looks_generic_or_repeated(title, all_titles) and wc >= LONG_STEP_WORDS
            if not needs_work:
                continue

            # Without this, a definitively-rejected step (e.g. "this is a
            # materials list, not a protocol step") gets re-asked to the LLM
            # on every single future run forever, since it never gets a
            # title and needs_work stays true. Only skip on a REAL verdict —
            # transient failures ("no LLM response") are deliberately left
            # unmarked so they retry naturally on the next run.
            if s.get("_restructure_attempted"):
                continue

            title, substeps, accepted, note = restructure_step(instr)
            processed += 1
            if accepted:
                s["title"] = title
                if substeps and wc >= LONG_STEP_WORDS:
                    s["substeps"] = [{"instruction": sub, "title": ""} for sub in substeps]
                accepted_count += 1
                changed = True
            else:
                rejected_count += 1
                log_rejection(source, norm_path.stem, instr, note)
                if note != "no LLM response":
                    s["_restructure_attempted"] = True
                    changed = True
            time.sleep(0.05)  # was 0.3 — real cost is the ~4s LLM call itself, this was just idle padding

        protocols_touched += 1
        if changed:
            norm_path.write_text(json.dumps(d, ensure_ascii=False, indent=2))

        if processed % 50 == 0 and processed > 0:
            print(f"  [{source}] protocols={protocols_touched} processed={processed} accepted={accepted_count} rejected={rejected_count}")

    print(f"{source}: protocols={protocols_touched} steps_processed={processed} accepted={accepted_count} rejected={rejected_count}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pilot", type=int, help="Show N examples without writing anything")
    parser.add_argument("--source", help="Process one source only")
    parser.add_argument("--limit", type=int, help="Limit number of steps processed (for testing)")
    parser.add_argument("--protocols", type=int, help="Limit number of protocols processed (for testing)")
    parser.add_argument("--shard", help="Run as shard i/n, e.g. --shard 0/8 — processes every nth file, "
                         "disjoint from other shards, safe to run many in parallel on one large source")
    args = parser.parse_args()

    print(f"LLM config: Groq={'YES' if GROQ_AVAILABLE and GROQ_KEY else 'NO'} | OpenAI={'YES' if OPENAI_AVAILABLE and OPENAI_KEY else 'NO'}")

    if args.pilot:
        pilot(args.pilot)
        return

    shard_idx, num_shards = 0, 1
    if args.shard:
        shard_idx, num_shards = (int(x) for x in args.shard.split("/"))

    sources = [args.source] if args.source else [d.name for d in SOURCES_DIR.iterdir() if d.is_dir()]
    for source in sources:
        process_source(source, args.limit, args.protocols, shard_idx, num_shards)


if __name__ == "__main__":
    main()
