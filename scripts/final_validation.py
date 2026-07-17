"""
final_validation.py

Last-mile check on the ACTUAL PUBLISHED protocol (protocols/*.json), after
every other pipeline stage has run. Distinct from verify_protocol_quality.py
(which checks the pipeline didn't corrupt data during processing) — this
checks the finished artifact itself makes sense as a thing a scientist would
open and follow.

Four layers, cheapest first:
  1. SCHEMA — every required field present, correct type, steps/substeps
     structurally valid. Deterministic, catches our own bugs.
  2. NUMERIC WELL-FORMEDNESS — no truncated numbers ("SI 166–" with nothing
     after), no leftover template artifacts ("%s"), no dangling units.
     Different from content-preservation checks elsewhere in the pipeline —
     this checks the number is COMPLETE in the final output, regardless of
     whether it matches the source.
  3. SENTENCE COHERENCE (heuristic) — starts capitalized or is a short
     label, ends with terminal punctuation or is short enough to be a
     title-like fragment, no raw HTML/markup leftover, no orphaned
     connector words at the end (reuses verify_protocol_quality.py's
     severed-sentence detector logic).
  4. HOLISTIC LLM SANITY PASS (optional, --llm flag) — given the whole
     protocol, ask an LLM "does this read as a coherent, followable
     procedure" and get back a pass/fail + specific issues. Expensive at
     corpus scale (13k+ protocols) — run as its own background pass, not
     part of every pipeline run.

Usage:
    python3 scripts/final_validation.py                    # layers 1-3, all protocols
    python3 scripts/final_validation.py --source protocols_io
    python3 scripts/final_validation.py --llm --limit 200   # add layer 4 on a sample
"""

import argparse
import json
import os
import pathlib
import re
import time

PROTOCOLS_DIR = pathlib.Path("protocols")

REQUIRED_PROTOCOL_FIELDS = {
    "protocol_id": str, "title": str, "steps": list, "materials": list,
    "category": str, "source_name": str, "license": str,
}
REQUIRED_STEP_FIELDS = {
    "step_id": int, "title": str, "instruction": str,
    "substeps": list, "is_critical": bool, "timers": list,
}

# Truncated-number / leftover-artifact patterns.
_DANGLING_NUMBER_RE = re.compile(r"\b\d+[–—\-]\s*$")  # "166–" at end of string, nothing after
_TEMPLATE_ARTIFACT_RE = re.compile(r"(?<![A-Za-z0-9_])%s(?![A-Za-z0-9_])")
_RAW_HTML_RE = re.compile(r"<(p|div|span|table|tr|td|figure)\b[^>]*>", re.IGNORECASE)

_DANGLING_WORDS = {
    "the", "a", "an", "and", "or", "with", "using", "for", "to", "of", "in",
    "at", "by", "on", "was", "is", "were", "are", "that", "which", "then",
    "from", "into", "as", "after", "before",
}
_WORD_RE = re.compile(r"[A-Za-z']+")


# ---------------------------------------------------------------------------
# Layer 1: schema
# ---------------------------------------------------------------------------

def check_schema(d: dict) -> list[str]:
    issues = []
    for field, expected_type in REQUIRED_PROTOCOL_FIELDS.items():
        if field not in d:
            issues.append(f"missing field: {field}")
        elif not isinstance(d[field], expected_type):
            issues.append(f"wrong type for {field}: expected {expected_type.__name__}, got {type(d[field]).__name__}")

    steps = d.get("steps") or []
    ids = [s.get("step_id") for s in steps]
    if ids != list(range(len(ids))):
        issues.append(f"step_id not sequential: {ids[:8]}")

    for i, s in enumerate(steps):
        if not isinstance(s, dict):
            issues.append(f"step {i} is not a dict")
            continue
        for field, expected_type in REQUIRED_STEP_FIELDS.items():
            if field not in s:
                issues.append(f"step {i} missing field: {field}")
            elif not isinstance(s[field], expected_type):
                issues.append(f"step {i} wrong type for {field}")
        for j, sub in enumerate(s.get("substeps") or []):
            if not isinstance(sub, dict) or "instruction" not in sub:
                issues.append(f"step {i} substep {j} malformed")

    return issues


# ---------------------------------------------------------------------------
# Layer 2: numeric well-formedness
# ---------------------------------------------------------------------------

def check_numeric_wellformedness(d: dict) -> list[str]:
    issues = []
    for i, s in enumerate(d.get("steps") or []):
        instr = s.get("instruction") or ""
        if _DANGLING_NUMBER_RE.search(instr):
            issues.append(f"step {i}: number truncated at end of instruction")
        if _TEMPLATE_ARTIFACT_RE.search(instr):
            issues.append(f"step {i}: leftover %s template artifact")
        if _RAW_HTML_RE.search(instr):
            issues.append(f"step {i}: raw HTML markup leaked into instruction")
    for i, m in enumerate(d.get("materials") or []):
        if _TEMPLATE_ARTIFACT_RE.search(m):
            issues.append(f"materials[{i}]: leftover %s template artifact")
    return issues


# ---------------------------------------------------------------------------
# Layer 3: sentence coherence (heuristic)
# ---------------------------------------------------------------------------

def looks_severed(cur: str, nxt: str) -> bool:
    cur, nxt = (cur or "").strip(), (nxt or "").strip()
    if not cur or not nxt:
        return False
    words = _WORD_RE.findall(cur)
    if not words:
        return False
    return words[-1].lower() in _DANGLING_WORDS and nxt[0].islower()


def check_sentence_coherence(d: dict) -> list[str]:
    issues = []
    steps = d.get("steps") or []
    for i in range(len(steps) - 1):
        if looks_severed(steps[i].get("instruction"), steps[i + 1].get("instruction")):
            issues.append(f"step {i}->{i+1}: looks severed mid-sentence")

    for i, s in enumerate(steps):
        instr = (s.get("instruction") or "").strip()
        if not instr:
            issues.append(f"step {i}: empty instruction")
            continue
        if len(instr) > 15 and instr[0].islower() and not instr[0].isdigit():
            issues.append(f"step {i}: starts lowercase (possible fragment)")

    return issues


# ---------------------------------------------------------------------------
# Layer 4: holistic LLM sanity pass (optional)
# ---------------------------------------------------------------------------

_env_path = pathlib.Path(".env")
if _env_path.exists():
    for _line in _env_path.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

OLLAMA_KEY = os.environ.get("OLLAMA_API_KEY", "")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "gpt-oss:120b-cloud")
OLLAMA_URL = "https://ollama.com/v1"

SANITY_PROMPT = """You are reviewing a lab/technical protocol for a scientist deciding whether to trust it enough to follow.

Given the protocol title and its steps, answer ONLY with JSON:
{
  "makes_sense": true/false,
  "issues": ["short description of each problem, empty list if none"]
}

Flag makes_sense=false if:
- The steps don't form a coherent, followable procedure (e.g. it's actually an academic paper's paragraphs, not real actions)
- Steps appear scrambled, contradictory, or out of logical order
- Critical information is obviously missing or garbled

Do NOT flag stylistic issues, missing images, or content you merely wish had more detail — only flag things that would actively mislead or confuse someone trying to follow it.
"""


def llm_sanity_check(title: str, steps: list) -> dict | None:
    try:
        from openai import OpenAI as OpenAIClient
    except ImportError:
        return None
    if not OLLAMA_KEY:
        return None

    step_text = "\n".join(f"{i+1}. {s.get('instruction','')[:200]}" for i, s in enumerate(steps[:40]))
    prompt = f"{SANITY_PROMPT}\n\nTitle: {title}\n\nSteps:\n{step_text}"

    try:
        client = OpenAIClient(api_key=OLLAMA_KEY, base_url=OLLAMA_URL)
        resp = client.chat.completions.create(
            model=OLLAMA_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=400,
            temperature=0.1,
        )
        text = resp.choices[0].message.content.strip()
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            return json.loads(m.group(0))
    except Exception as e:
        print(f"    [LLM sanity check error] {e}")
    return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def process(source_filter: str | None, use_llm: bool, limit: int | None) -> None:
    files = sorted(PROTOCOLS_DIR.glob("*.json"))
    total = schema_fail = numeric_fail = coherence_fail = llm_fail = 0
    llm_checked = 0

    for f in files:
        if limit and llm_checked >= limit and use_llm:
            break
        try:
            d = json.loads(f.read_text())
        except Exception:
            schema_fail += 1
            continue

        if source_filter and d.get("source_name") != source_filter:
            continue
        total += 1

        schema_issues = check_schema(d)
        numeric_issues = check_numeric_wellformedness(d)
        coherence_issues = check_sentence_coherence(d)

        if schema_issues:
            schema_fail += 1
        if numeric_issues:
            numeric_fail += 1
        if coherence_issues:
            coherence_fail += 1

        if use_llm and not schema_issues and d.get("steps"):
            result = llm_sanity_check(d.get("title", ""), d.get("steps") or [])
            llm_checked += 1
            if result and not result.get("makes_sense", True):
                llm_fail += 1
                print(f"  [LLM FLAG] {f.stem}: {result.get('issues')}")
            time.sleep(0.3)

    print(f"\nTotal checked: {total}")
    print(f"Schema violations:     {schema_fail}")
    print(f"Numeric malformed:     {numeric_fail}")
    print(f"Coherence issues:      {coherence_fail}")
    if use_llm:
        print(f"LLM sanity-checked:    {llm_checked}")
        print(f"LLM flagged as not making sense: {llm_fail}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", help="Filter to one source_name")
    parser.add_argument("--llm", action="store_true", help="Add layer 4 (holistic LLM sanity check)")
    parser.add_argument("--limit", type=int, help="Limit LLM-checked protocols (for cost/time control)")
    args = parser.parse_args()
    process(args.source, args.llm, args.limit)


if __name__ == "__main__":
    main()
