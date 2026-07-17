"""
classify_top20.py
Run the top 20 complex protocols through glm-4.7 on Ollama Cloud.
Classifies every block of text into: background | materials | equipment |
before_you_begin | step | sub_protocol | expected_result | tip | warning |
limitation | figure | note

Saves: data/classified/{filename}.json
"""
import json, os, re, sys, time, requests
import xml.etree.ElementTree as ET
from pathlib import Path

BASE     = Path(__file__).parent.parent
RAW      = BASE / "data/raw"
ANALYSIS = BASE / "data/complexity_analysis.json"
OUT      = BASE / "data/classified"
OUT.mkdir(exist_ok=True)

GROQ_KEY = os.environ.get("OPENAI_API_KEY", "")
MODEL    = "gpt-4o-mini"
API_URL  = "https://api.openai.com/v1/chat/completions"

SYSTEM_PROMPT = """You are a scientific protocol parser. You will receive raw text blocks from a scientific protocol.
Your job is to classify each block into exactly one of these types:

- background: introduction, rationale, overview, context, why this is done
- before_you_begin: prerequisites, preparation notes, what to set up first
- materials: reagents, chemicals, solutions, buffers — things you need to have
- equipment: instruments, machines, tools, software
- step: an actual procedural action the scientist performs
- sub_protocol: a named sub-procedure (e.g. "Prepare Buffer A", "Day 1 setup")
- expected_result: what you should observe or see after a step
- tip: helpful hints, tricks, notes on technique
- warning: safety warnings, critical cautions
- limitation: known issues, caveats, what this protocol doesn't cover
- figure: description of a figure or image
- note: anything else that doesn't fit above

Return ONLY valid JSON in this exact format, no other text:
{
  "blocks": [
    {
      "type": "step",
      "label": "one sentence explaining why you classified it this way",
      "text": "the original text verbatim"
    }
  ]
}"""

def clean(s):
    return re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', str(s or ''))).strip()

def extract_blocks_pmc(xml_bytes):
    """Extract raw text blocks from PMC XML preserving structure."""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return []

    blocks = []

    # Abstract
    abstract = root.find(".//abstract")
    if abstract is not None:
        t = ET.tostring(abstract, encoding="unicode", method="text").strip()
        t = re.sub(r'\s+', ' ', t).strip()
        if t:
            blocks.append({"source_type": "abstract", "text": t})

    # Body sections
    def process_sec(sec, depth=0):
        title_el = sec.find("title")
        heading = ET.tostring(title_el, encoding="unicode", method="text").strip() if title_el is not None else ""

        for child in sec:
            tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
            if tag == "sec":
                process_sec(child, depth + 1)
            elif tag == "p":
                t = ET.tostring(child, encoding="unicode", method="text").strip()
                t = re.sub(r'\s+', ' ', t).strip()
                if t and len(t) > 20:
                    blocks.append({"source_type": "paragraph", "heading": heading, "text": t})
            elif tag == "list":
                items = []
                for li in child.findall(".//list-item"):
                    t = ET.tostring(li, encoding="unicode", method="text").strip()
                    t = re.sub(r'\s+', ' ', t).strip()
                    if t:
                        items.append(t)
                if items:
                    blocks.append({"source_type": "list", "heading": heading, "items": items, "text": "\n".join(f"- {i}" for i in items)})
            elif tag == "fig":
                label = child.find("label")
                caption = child.find(".//caption/p")
                fig_text = ""
                if label is not None and label.text:
                    fig_text += label.text + ": "
                if caption is not None:
                    fig_text += ET.tostring(caption, encoding="unicode", method="text").strip()
                if fig_text:
                    blocks.append({"source_type": "figure", "text": fig_text})

    body = root.find(".//body")
    if body is not None:
        for sec in body.findall("sec"):
            process_sec(sec)

    return blocks

def extract_blocks_pio(data):
    """Extract raw text blocks from protocols.io JSON."""
    proto = data.get("protocol") or data.get("item") or data
    if not proto:
        return []

    blocks = []

    for field, label in [("description","description"), ("before_start","before_start"),
                          ("guidelines","guidelines"), ("warning","warning")]:
        t = clean(proto.get(field, ""))
        if t:
            blocks.append({"source_type": field, "text": t})

    # Materials list
    mats = proto.get("materials") or []
    if mats:
        lines = []
        for m in mats:
            vendor = (m.get("vendor") or {}).get("name", "")
            sku = m.get("sku", "")
            line = m.get("name", "")
            if vendor: line += f" — {vendor}"
            if sku: line += f" (#{sku})"
            lines.append(line)
        blocks.append({"source_type": "materials_list", "text": "\n".join(lines)})

    # Steps
    steps = proto.get("steps") or []
    type_map = {1:"text", 6:"section_header", 2:"expected_result", 3:"reagent",
                5:"equipment", 7:"safety", 8:"tip"}
    for step in steps:
        comps = step.get("components") or []
        step_parts = []
        for c in comps:
            tid = c.get("type_id")
            src = c.get("source") or {}
            val = clean(src.get("description") or src.get("body") or src.get("title") or src.get("name") or "")
            if val:
                step_parts.append({"component_type": type_map.get(tid, f"type_{tid}"), "text": val})
        if step_parts:
            combined = " | ".join(p["text"] for p in step_parts)
            blocks.append({"source_type": "step", "step_number": step.get("number",""), "text": combined, "components": step_parts})

    return blocks

def classify_one(block, title):
    """Classify a single block — simple, reliable, no JSON truncation."""
    text = block.get("text", "")[:2000]  # truncate very long blocks
    source_type = block.get("source_type", "")

    prompt = f"""Protocol: "{title}"
Section hint: {source_type}
Text: {text}

Classify this text block into exactly one type: background, before_you_begin, materials, equipment, step, sub_protocol, expected_result, tip, warning, limitation, figure, note

Reply ONLY with JSON (no markdown): {{"type": "step", "label": "one-line reason"}}"""

    for attempt in range(4):
        try:
            r = requests.post(
                API_URL,
                headers={"Authorization": f"Bearer {GROQ_KEY}", "Content-Type": "application/json"},
                json={
                    "model": MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 60,
                    "temperature": 0.1
                },
                timeout=30
            )
            if r.status_code == 429:
                reset = r.headers.get("x-ratelimit-reset-tokens", "60s")
                secs = int(re.search(r"(\d+)", reset).group(1)) + 5 if re.search(r"(\d+)", reset) else 60
                wait = max(secs, (attempt + 1) * 15)
                print(f"  [429] waiting {wait}s")
                time.sleep(wait)
                continue
            if r.status_code != 200:
                print(f"  [ERROR] {r.status_code} {r.text[:80]}")
                return {"classified_type": "note", "classified_label": f"API error {r.status_code}"}

            msg = r.json()["choices"][0]["message"]
            content = msg.get("content") or msg.get("reasoning") or ""
            content = re.sub(r"```(?:json)?", "", content).strip()
            m = re.search(r'\{[^}]+\}', content)
            if m:
                result = json.loads(m.group())
                return {
                    "classified_type": result.get("type", "note"),
                    "classified_label": result.get("label", ""),
                }
            return {"classified_type": "note", "classified_label": "no JSON in response"}

        except Exception as e:
            if attempt < 3:
                time.sleep(5)
            else:
                return {"classified_type": "note", "classified_label": str(e)[:60]}

    return {"classified_type": "note", "classified_label": "max retries"}

def classify_blocks(blocks, title):
    """Classify each block individually."""
    classified = []
    for i, block in enumerate(blocks):
        result = classify_one(block, title)
        classified.append({**block, **result})
        time.sleep(0.3)
    return classified

def main():
    if not GROQ_KEY:
        print("[FATAL] OPENAI_API_KEY not set"); sys.exit(1)

    ranked = json.loads(ANALYSIS.read_text()).get("top_100_ranked", [])
    top20 = ranked[:20]

    print(f"Classifying top 20 complex protocols using {MODEL}\n")

    for entry in top20:
        rank = entry["rank"]
        source = entry["source"]
        filename = entry["filename"]
        title = entry["title"]
        out_file = OUT / f"{filename}.json"

        if out_file.exists():
            print(f"  [SKIP] #{rank} {filename} — already done")
            continue

        print(f"  [{rank}/20] {source}/{filename}")
        print(f"          {title[:70]}")

        if source == "pmc":
            fpath = RAW / "pmc" / filename
            if not fpath.exists():
                print(f"  [SKIP] file not found"); continue
            blocks = extract_blocks_pmc(fpath.read_bytes())

        elif source == "protocols_io":
            fpath = RAW / "protocols_io" / filename
            if not fpath.exists():
                print(f"  [SKIP] file not found"); continue
            blocks = extract_blocks_pio(json.loads(fpath.read_bytes()))

        else:
            print(f"  [SKIP] unsupported source {source}"); continue

        print(f"          {len(blocks)} blocks extracted — classifying...")
        classified = classify_blocks(blocks, title)
        print(f"          {len(classified)} blocks classified")

        out_file.write_text(json.dumps({
            "rank": rank,
            "source": source,
            "filename": filename,
            "title": title,
            "breakdown": entry.get("breakdown", {}),
            "blocks": classified
        }, ensure_ascii=False, indent=2))

        print(f"          saved → data/classified/{filename}.json\n")
        time.sleep(2)

    print("Done.")

if __name__ == "__main__":
    main()
