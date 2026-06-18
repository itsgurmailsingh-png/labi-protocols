"""
fill_step_titles.py — Generate 3-5 word action titles for steps that have none.

Reads normalised files, finds steps with empty titles, batches them to Groq,
writes titles back in place.

Usage:
    python3 scripts/fill_step_titles.py
    python3 scripts/fill_step_titles.py --limit 100   # process N files only
"""

import json
import os
import re
import time
import pathlib
import argparse

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

GROQ_KEY   = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.1-8b-instant")
OPENAI_KEY = os.environ.get("OPENAI_API_KEY", "")
SOURCES_DIR = pathlib.Path("data/sources")
DELAY_SECS  = 0.3


SYSTEM_PROMPT = """You are a lab protocol assistant. Given a list of step instructions, return ONLY a JSON array of short action titles — one per step, in order.

Rules:
- 3-6 words max per title
- Start with a verb (Add, Centrifuge, Incubate, Prepare, Mix, etc.)
- Plain text only, no markdown
- Return exactly as many titles as there are instructions

Example input:
["Add 50mg tissue to tube with 500µl buffer.", "Vortex 5 seconds.", "Centrifuge at 12000g for 10 min."]

Example output:
["Add Tissue to Buffer", "Vortex Sample Briefly", "Centrifuge at High Speed"]
"""


def _call_llm(instructions: list[str]) -> list[str] | None:
    prompt = f"{SYSTEM_PROMPT}\n\nInstructions:\n{json.dumps(instructions, ensure_ascii=False)}"

    if GROQ_AVAILABLE and GROQ_KEY:
        try:
            client = GroqClient(api_key=GROQ_KEY)
            resp = client.chat.completions.create(
                model=GROQ_MODEL,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=500,
                temperature=0.1,
            )
            text = resp.choices[0].message.content.strip()
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text)
            match = re.search(r"\[.*\]", text, re.DOTALL)
            if match:
                titles = json.loads(match.group(0))
                if isinstance(titles, list):
                    return [str(t) for t in titles]
        except Exception as e:
            print(f"  Groq error: {e}", end=" ")

    if OPENAI_AVAILABLE and OPENAI_KEY:
        try:
            client = OpenAIClient(api_key=OPENAI_KEY)
            resp = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=500,
                temperature=0.1,
            )
            text = resp.choices[0].message.content.strip()
            match = re.search(r"\[.*\]", text, re.DOTALL)
            if match:
                titles = json.loads(match.group(0))
                if isinstance(titles, list):
                    return [str(t) for t in titles]
        except Exception as e:
            print(f"  OpenAI error: {e}", end=" ")

    return None


def process_file(path: pathlib.Path) -> bool:
    d = json.loads(path.read_text())
    steps = d.get("steps", [])

    # Find steps missing titles
    empty_indices = [i for i, s in enumerate(steps) if not s.get("title", "").strip()]
    if not empty_indices:
        return False  # nothing to do

    instructions = [steps[i].get("instruction", "")[:200] for i in empty_indices]

    titles = _call_llm(instructions)
    if not titles or len(titles) != len(empty_indices):
        # Fallback: first 5 words of instruction
        titles = []
        for instr in instructions:
            words = instr.strip().split()[:6]
            titles.append(" ".join(words) if words else "Step")

    for i, title in zip(empty_indices, titles):
        steps[i]["title"] = title.strip()

    d["steps"] = steps
    path.write_text(json.dumps(d, ensure_ascii=False, indent=2))
    return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0, help="Max files to process (0 = all)")
    parser.add_argument("--source", default="protocols_io")
    args = parser.parse_args()

    if not GROQ_KEY and not OPENAI_KEY:
        print("No API keys — will use word-extraction fallback only")

    nd = SOURCES_DIR / args.source / "normalised"
    files = sorted(nd.glob("*.json"))
    if args.limit:
        files = files[:args.limit]

    processed = skipped = 0
    for path in files:
        try:
            changed = process_file(path)
            if changed:
                print(f"  Titled: {path.name[:60]}")
                processed += 1
                time.sleep(DELAY_SECS)
            else:
                skipped += 1
        except Exception as e:
            print(f"  [ERROR] {path.name}: {e}")

    print(f"\nDone: {processed} files updated, {skipped} already had titles")


if __name__ == "__main__":
    main()
