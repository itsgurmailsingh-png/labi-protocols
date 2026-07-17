"""
restructure_whole_protocol.py

Replaces the per-step restructuring approach: instead of sending one
already-heuristically-cut step at a time and only polishing its title,
this sends a protocol's FULL raw content (title + every current
step/paragraph, in original order) in ONE call and lets the model decide
the actual step boundaries, titles, and substeps from scratch — the model
owns segmentation, not a crude XML-paragraph-per-step heuristic.

Why this replaces restructure_steps_llm.py's per-step mode:
- ~7x fewer calls (one per protocol instead of one per step needing work)
- The model sees the whole document, so step boundaries and titles are
  coherent with each other instead of judged in isolation
- It actually fixes bad heuristic step-cuts instead of polishing on top of
  them (methodsx's XML-paragraph-per-step splitting has real boundary
  errors this can correct)

Same self-report safety pattern as before (is_protocol / content_fully_
preserved / notes) but at the whole-protocol level, and every rejection is
logged with its reason for audit — nothing silently discarded.

Usage:
    python3 scripts/restructure_whole_protocol.py --pilot 5
    python3 scripts/restructure_whole_protocol.py --source methodsx --protocols 20
    python3 scripts/restructure_whole_protocol.py --source methodsx --shard 0/8
"""

import argparse
import json
import random
import os
import pathlib
import re
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
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "gemma4:31b")
OLLAMA_URL   = "https://ollama.com/v1"
GROQ_KEY     = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL   = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
OPENAI_KEY   = os.environ.get("OPENAI_API_KEY", "")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")

SOURCES_DIR = pathlib.Path("data/sources")
REQUEST_TIMEOUT = 120.0  # gemma4:31b is non-reasoning and 4x smaller than gpt-oss:120b, should not need 240s
MAX_OUTPUT_TOKENS = 8000

# max_retries=0: the SDK's own default (2) retries transparently on top of
# our explicit 429-backoff loop below, so a single stuck call could silently
# consume up to 3x REQUEST_TIMEOUT before an exception ever reached our code
# (confirmed live: processes sitting at 600s+ etime with zero completions).
_ollama_client = OpenAIClient(api_key=OLLAMA_KEY, base_url=OLLAMA_URL, timeout=REQUEST_TIMEOUT, max_retries=0) if OPENAI_AVAILABLE and OLLAMA_KEY else None
_groq_client = GroqClient(api_key=GROQ_KEY, timeout=REQUEST_TIMEOUT, max_retries=0) if GROQ_AVAILABLE and GROQ_KEY else None
_openai_client = OpenAIClient(api_key=OPENAI_KEY, timeout=REQUEST_TIMEOUT, max_retries=0) if OPENAI_AVAILABLE and OPENAI_KEY else None

SYSTEM_PROMPT = """You are given the RAW content of a lab protocol article: its title and every extracted paragraph in original document order. These paragraphs were mechanically cut by XML structure, NOT by meaning — some may need to be merged (one real step split across two paragraphs), split apart (one paragraph describing several distinct actions), or dropped entirely (background/literature/administrative text that isn't a step at all).

Read the WHOLE thing and produce the correct, complete step-by-step breakdown a bench scientist could actually follow.

Return ONLY a JSON object:
{
  "is_protocol": true or false — does this content actually describe an experimental/lab procedure at all (not a review article, editorial, or purely theoretical paper)?
  "steps": [
    {
      "title": "short imperative title, max 8 words",
      "instruction": "the main instruction text for this step, in your own concise words but preserving every technical detail",
      "substeps": ["ordered list of sub-actions if this step has multiple distinct parts, else empty list"]
    }
  ],
  "content_fully_preserved": true or false — true only if EVERY number, quantity, unit, reagent name, temperature, duration, concentration, and technical detail from the original paragraphs appears somewhere in your steps. Be honest — if you had to drop or round anything, say false.
  "notes": "If content_fully_preserved is false or is_protocol is false, explain exactly why/what was dropped. Empty string otherwise."
}

RULES:
- Merge paragraphs that are really one step. Split apart any paragraph describing multiple distinct actions (or use substeps within one step instead).
- Drop background/introduction/literature-review/administrative/acknowledgment paragraphs — they are not steps. But never drop a paragraph that contains an action or a technical parameter (temperature, volume, timing, reagent, catalog number).
- Order steps the way a scientist would actually perform them, following the original document order unless it's clearly wrong.
- Do not invent, infer, or add information not in the original text.
"""

_TOKEN_RE = re.compile(
    r"\d{1,3}(?:,\d{3})*(?:\.\d+)?\s*(?:µL|uL|mL|L|mg|g|kg|µg|ng|mM|M|nM|°C|°F|min|sec|s\b|h\b|hr|rpm|x\s*g|%|bp|kb|nm|µm|mm|cm)?",
    re.IGNORECASE,
)


def build_raw_text(d: dict) -> str:
    parts = [d.get("title", "")]
    for s in d.get("steps") or []:
        title = (s.get("title") or "").strip()
        instr = (s.get("instruction") or "").strip()
        if title and title.lower() not in instr.lower()[:len(title) + 20]:
            parts.append(f"{title}: {instr}")
        else:
            parts.append(instr)
    return "\n\n".join(p for p in parts if p)


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


def call_llm(raw_text: str) -> dict | None:
    prompt = f"{SYSTEM_PROMPT}\n\nProtocol content:\n{raw_text}"

    if _ollama_client:
        # 29 shards share one Ollama Cloud account, so "too many concurrent
        # requests" (429) is routine, not exceptional — retry with backoff
        # instead of falling straight through to the (quota-exhausted) OpenAI
        # fallback, which was turning transient collisions into permanent
        # "no LLM response" failures for the majority of attempts.
        for attempt in range(4):
            try:
                resp = _ollama_client.chat.completions.create(
                    model=OLLAMA_MODEL,
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=MAX_OUTPUT_TOKENS,
                    temperature=0.1,
                    timeout=REQUEST_TIMEOUT,
                )
                if resp.choices[0].finish_reason == "length":
                    print("    [WARN] Ollama response truncated (finish_reason=length)")
                    return None
                result = _parse(resp.choices[0].message.content)
                if result:
                    return result
                break
            except Exception as e:
                is_concurrency_limit = "too many concurrent requests" in str(e).lower() or "429" in str(e)
                if is_concurrency_limit and attempt < 3:
                    delay = (2 ** attempt) + random.uniform(0, 2)
                    print(f"    [Ollama Cloud 429] retrying in {delay:.1f}s (attempt {attempt + 1}/4)")
                    time.sleep(delay)
                    continue
                print(f"    [Ollama Cloud error] {e}")
                break

    # Groq's free tier hard-caps at 6000 tokens/min for this model. chars//4
    # badly undercounts dense scientific text (confirmed live: prompts we
    # estimated under 5000 tokens were reported by Groq's own tokenizer as
    # 9900-12600) — so use a much more conservative ratio and threshold.
    estimated_tokens = len(prompt) // 3
    if _groq_client and estimated_tokens < 1500:
        try:
            resp = _groq_client.chat.completions.create(
                model=GROQ_MODEL,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=MAX_OUTPUT_TOKENS,
                temperature=0.1,
                timeout=REQUEST_TIMEOUT,
            )
            result = _parse(resp.choices[0].message.content)
            if result:
                return result
        except Exception as e:
            print(f"    [Groq error] {e}")

    # OpenAI fallback confirmed dead all session (insufficient_quota on every
    # single call, regardless of content) — calling it is a pure wasted
    # round trip on the failure path of every rejected file. Disabled.
    return None

    return None


REJECTIONS_LOG = pathlib.Path("logs/restructure_whole_protocol_rejections.jsonl")


def log_rejection(source: str, protocol_id: str, note: str) -> None:
    REJECTIONS_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(REJECTIONS_LOG, "a") as f:
        f.write(json.dumps({"source": source, "protocol_id": protocol_id, "note": note}, ensure_ascii=False) + "\n")


def restructure_protocol(d: dict) -> tuple[list, bool, str]:
    """Returns (new_steps, accepted, note)."""
    raw_text = build_raw_text(d)
    result = call_llm(raw_text)
    if not result:
        return [], False, "no LLM response"

    is_protocol = result.get("is_protocol", True)
    if not is_protocol:
        return [], False, (result.get("notes") or "model flagged: not a real protocol")

    raw_steps = result.get("steps") or []
    if not raw_steps:
        return [], False, "model returned zero steps"

    content_ok = result.get("content_fully_preserved", True)
    note = (result.get("notes") or "").strip()
    if not content_ok:
        return [], False, note or "model self-reported incomplete content preservation"

    new_steps = []
    for i, s in enumerate(raw_steps):
        title = (s.get("title") or "").strip()
        instr = (s.get("instruction") or "").strip()
        if not instr:
            continue
        substeps = [{"instruction": sub.strip(), "title": ""} for sub in (s.get("substeps") or []) if isinstance(sub, str) and sub.strip()]
        new_steps.append({
            "step_id": len(new_steps),
            "title": title,
            "instruction": instr,
            "substeps": substeps,
            "is_critical": any(w in instr.lower() for w in ["critical", "immediately", "do not", "must not"]),
            "timers": [],
        })

    if not new_steps:
        return [], False, "all returned steps were empty after cleanup"

    return new_steps, True, note


def pilot(source: str, n: int) -> None:
    files = sorted((SOURCES_DIR / source / "normalised").glob("*.json"))
    shown = 0
    for f in files:
        d = json.loads(f.read_text())
        if not d.get("steps"):
            continue
        print(f"=== {f.name} — {d.get('title','')[:60]} ===")
        print(f"ORIGINAL: {len(d['steps'])} steps")
        new_steps, accepted, note = restructure_protocol(d)
        print(f"RESULT: {'ACCEPTED' if accepted else 'REJECTED'}{f' — {note}' if note else ''}")
        if accepted:
            print(f"  NEW: {len(new_steps)} steps")
            for s in new_steps[:6]:
                print(f"    - {s['title']}: {s['instruction'][:80]}")
        print()
        shown += 1
        if shown >= n:
            break


def process_source(source: str, protocol_limit: int | None, shard: int = 0, num_shards: int = 1) -> None:
    norm_dir = SOURCES_DIR / source / "normalised"
    if not norm_dir.exists():
        print(f"[SKIP] {source}: no normalised/ dir")
        return

    files = sorted(norm_dir.glob("*.json"))
    if num_shards > 1:
        files = files[shard::num_shards]

    processed = accepted_count = rejected_count = 0
    for norm_path in files:
        if protocol_limit and processed >= protocol_limit:
            break
        try:
            d = json.loads(norm_path.read_text())
        except Exception:
            continue
        if not d.get("steps"):
            continue
        if d.get("_whole_protocol_attempted"):
            continue

        new_steps, accepted, note = restructure_protocol(d)
        processed += 1
        if accepted:
            d["steps"] = new_steps
            d["_whole_protocol_attempted"] = True
            accepted_count += 1
            norm_path.write_text(json.dumps(d, ensure_ascii=False, indent=2))
        else:
            rejected_count += 1
            log_rejection(source, norm_path.stem, note)
            if note != "no LLM response":
                d["_whole_protocol_attempted"] = True
                norm_path.write_text(json.dumps(d, ensure_ascii=False, indent=2))

        if processed % 20 == 0:
            print(f"  [{source}] processed={processed} accepted={accepted_count} rejected={rejected_count}")
        time.sleep(0.05)

    print(f"{source}: processed={processed} accepted={accepted_count} rejected={rejected_count}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pilot", type=int)
    parser.add_argument("--source", required=False)
    parser.add_argument("--protocols", type=int)
    parser.add_argument("--shard")
    args = parser.parse_args()

    print(f"LLM config: Ollama={'YES' if _ollama_client else 'NO'} | Groq={'YES' if _groq_client else 'NO'} | OpenAI={'YES' if _openai_client else 'NO'}")

    if args.pilot:
        pilot(args.source or "methodsx", args.pilot)
        return

    shard_idx, num_shards = 0, 1
    if args.shard:
        shard_idx, num_shards = (int(x) for x in args.shard.split("/"))

    sources = [args.source] if args.source else [d.name for d in SOURCES_DIR.iterdir() if d.is_dir()]
    for source in sources:
        process_source(source, args.protocols, shard_idx, num_shards)


if __name__ == "__main__":
    main()
