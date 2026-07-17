"""
protocol_reader.py
Local web interface — raw protocol vs classified view side by side.
Run: python3 scripts/protocol_reader.py
Open: http://localhost:7070
"""
import json, re, html, os
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
import xml.etree.ElementTree as ET

BASE       = Path(__file__).parent.parent
RAW        = BASE / "data/raw"
CLASSIFIED = BASE / "data/classified"
ANALYSIS   = BASE / "data/complexity_analysis.json"

def load_analysis():
    d = json.loads(ANALYSIS.read_text())
    return d.get("top_100_ranked", [])

def clean(s):
    s = re.sub(r"<[^>]+>", " ", str(s or ""))
    return re.sub(r"\s+", " ", s).strip()

# ── raw extractors ────────────────────────────────────────────────────────────

def raw_blocks_pmc(xml_bytes):
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return [{"heading": "Error", "text": "XML parse error"}]
    blocks = []
    abstract = root.find(".//abstract")
    if abstract is not None:
        t = re.sub(r'\s+', ' ', ET.tostring(abstract, encoding="unicode", method="text")).strip()
        if t: blocks.append({"heading": "Abstract", "text": t})

    def process_sec(sec, depth=0):
        title_el = sec.find("title")
        heading = ET.tostring(title_el, encoding="unicode", method="text").strip() if title_el is not None else ""
        for child in sec:
            tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
            if tag == "sec":
                process_sec(child, depth+1)
            elif tag == "p":
                t = re.sub(r'\s+', ' ', ET.tostring(child, encoding="unicode", method="text")).strip()
                if t and len(t) > 10:
                    blocks.append({"heading": heading, "depth": depth, "text": t})
            elif tag == "list":
                items = [re.sub(r'\s+', ' ', ET.tostring(li, encoding="unicode", method="text")).strip()
                         for li in child.findall(".//list-item")]
                items = [i for i in items if i]
                if items:
                    blocks.append({"heading": heading, "depth": depth, "text": "\n".join(f"• {i}" for i in items)})
            elif tag == "fig":
                label = child.find("label")
                caption = child.find(".//caption/p")
                t = (label.text or "Figure") if label is not None else "Figure"
                if caption is not None:
                    t += ": " + ET.tostring(caption, encoding="unicode", method="text").strip()
                blocks.append({"heading": "[FIGURE]", "text": t})
    body = root.find(".//body")
    if body is not None:
        for sec in body.findall("sec"):
            process_sec(sec)
    return blocks

def raw_blocks_pio(data):
    proto = data.get("protocol") or data.get("item") or data
    if not proto: return []
    blocks = []
    for field, label in [("description","Description"),("before_start","Before You Begin"),
                          ("guidelines","Guidelines"),("warning","Warning")]:
        t = clean(proto.get(field,""))
        if t: blocks.append({"heading": label, "text": t})
    mats = proto.get("materials") or []
    if mats:
        lines = []
        for m in mats:
            vendor = (m.get("vendor") or {}).get("name","")
            sku = m.get("sku","")
            line = m.get("name","")
            if vendor: line += f" — {vendor}"
            if sku: line += f" (#{sku})"
            lines.append(line)
        blocks.append({"heading": "Materials", "text": "\n".join(lines)})
    steps = proto.get("steps") or []
    type_map = {1:"text",6:"section_header",2:"expected_result",3:"reagent",5:"equipment",7:"safety",8:"tip"}
    for step in steps:
        comps = step.get("components") or []
        parts = []
        for c in comps:
            src = c.get("source") or {}
            val = clean(src.get("description") or src.get("body") or src.get("title") or src.get("name") or "")
            if val: parts.append(val)
        if parts:
            blocks.append({"heading": f"Step {step.get('number','')}", "text": " | ".join(parts)})
    return blocks

# ── HTML ──────────────────────────────────────────────────────────────────────

TYPE_COLORS = {
    "background":      ("#3b4fd4", "#1e2460"),
    "before_you_begin":("#7c3aed", "#2e1a5e"),
    "materials":       ("#0891b2", "#0c3547"),
    "equipment":       ("#0369a1", "#0c2d47"),
    "step":            ("#16a34a", "#0f3320"),
    "sub_protocol":    ("#ca8a04", "#3d2a00"),
    "expected_result": ("#0d9488", "#073d3a"),
    "tip":             ("#d97706", "#3d2600"),
    "warning":         ("#dc2626", "#3d0a0a"),
    "limitation":      ("#9333ea", "#2d1060"),
    "figure":          ("#6b7280", "#1f2937"),
    "note":            ("#6b7280", "#1f2937"),
}

CSS = """
:root{--bg:#0f1117;--surface:#1a1d27;--surface2:#22263a;--border:#2e3350;
      --text:#e8eaf6;--muted:#7b82b0;--accent:#6c8eff}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;font-size:13px;line-height:1.6}
.shell{display:flex;height:100vh;overflow:hidden}
.sidebar{width:300px;flex-shrink:0;background:var(--surface);border-right:1px solid var(--border);display:flex;flex-direction:column;overflow:hidden}
.main{flex:1;overflow:hidden;display:flex;flex-direction:column}
.sidebar-header{padding:16px 14px 10px;border-bottom:1px solid var(--border)}
.sidebar-header h1{font-size:.9rem;font-weight:700}
.sidebar-header p{font-size:.68rem;color:var(--muted);margin-top:3px}
.search-wrap{padding:8px 10px;border-bottom:1px solid var(--border)}
.search-wrap input{width:100%;background:var(--surface2);border:1px solid var(--border);color:var(--text);padding:6px 9px;border-radius:5px;font-size:.75rem;outline:none}
.protocol-list{overflow-y:auto;flex:1}
.proto-item{padding:9px 12px;border-bottom:1px solid var(--border);cursor:pointer;transition:background .1s}
.proto-item:hover{background:var(--surface2)}
.proto-item.active{background:var(--surface2);border-left:3px solid var(--accent)}
.proto-rank{font-size:.62rem;color:var(--muted);font-weight:700}
.proto-title{font-size:.75rem;margin-top:2px;line-height:1.3}
.proto-meta{font-size:.64rem;color:var(--muted);margin-top:3px;display:flex;gap:6px;flex-wrap:wrap;align-items:center}
.badge{padding:2px 6px;border-radius:999px;font-size:.6rem;font-weight:700}
.badge-pmc{background:rgba(108,142,255,.15);color:#6c8eff}
.badge-pio{background:rgba(167,139,250,.15);color:#a78bfa}
.classified-badge{background:rgba(74,222,128,.15);color:#4ade80}

/* split pane */
.split{display:flex;flex:1;overflow:hidden;gap:0}
.pane{flex:1;overflow-y:auto;padding:20px;border-right:1px solid var(--border)}
.pane:last-child{border-right:none}
.pane-header{font-size:.65rem;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);margin-bottom:14px;padding-bottom:8px;border-bottom:1px solid var(--border);position:sticky;top:0;background:var(--bg);z-index:1}
.proto-title-bar{padding:14px 20px;border-bottom:1px solid var(--border);background:var(--surface)}
.proto-title-bar h2{font-size:.95rem;font-weight:700}
.proto-title-bar p{font-size:.7rem;color:var(--muted);margin-top:3px}

/* raw blocks */
.raw-block{margin-bottom:12px;border:1px solid var(--border);border-radius:6px;overflow:hidden}
.raw-heading{font-size:.65rem;font-weight:700;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);background:var(--surface2);padding:5px 10px;border-bottom:1px solid var(--border)}
.raw-text{font-size:.78rem;color:var(--text);padding:9px 10px;white-space:pre-wrap;line-height:1.65}

/* classified blocks */
.cls-block{margin-bottom:10px;border-radius:6px;overflow:hidden}
.cls-header{padding:5px 10px;display:flex;align-items:center;gap:8px}
.cls-type{font-size:.62rem;font-weight:700;text-transform:uppercase;letter-spacing:.07em}
.cls-label{font-size:.65rem;opacity:.8;font-style:italic}
.cls-text{font-size:.78rem;padding:9px 10px;white-space:pre-wrap;line-height:1.65;color:var(--text)}
.pending{padding:40px;text-align:center;color:var(--muted);font-size:.82rem}
.empty{padding:60px;text-align:center;color:var(--muted)}
.legend{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:14px}
.legend-item{display:flex;align-items:center;gap:4px;font-size:.62rem}
.legend-dot{width:8px;height:8px;border-radius:50%}
"""

TYPE_COLORS_CSS = {k: v[0] for k, v in TYPE_COLORS.items()}
TYPE_BG_CSS = {k: v[1] for k, v in TYPE_COLORS.items()}

def legend_html():
    items = ""
    for t, (fg, bg) in TYPE_COLORS.items():
        items += f'<div class="legend-item"><div class="legend-dot" style="background:{fg}"></div><span style="color:{fg}">{t}</span></div>'
    return f'<div class="legend">{items}</div>'

def render_raw_pane(blocks):
    if not blocks:
        return '<div class="pending">No raw blocks extracted.</div>'
    parts = []
    for b in blocks:
        heading = html.escape(b.get("heading",""))
        text = html.escape(b.get("text",""))
        h = f'<div class="raw-heading">{heading}</div>' if heading else ""
        parts.append(f'<div class="raw-block">{h}<div class="raw-text">{text}</div></div>')
    return "".join(parts)

def render_classified_pane(classified_data):
    if classified_data is None:
        return '<div class="pending">⏳ Not yet classified — run classify_top20.py to generate classifications.</div>'
    blocks = classified_data.get("blocks", [])
    if not blocks:
        return '<div class="pending">No classified blocks.</div>'
    parts = [legend_html()]
    for b in blocks:
        ctype = b.get("classified_type","note")
        fg = TYPE_COLORS_CSS.get(ctype, "#6b7280")
        bg = TYPE_BG_CSS.get(ctype, "#1f2937")
        clabel = html.escape(b.get("classified_label",""))
        text = html.escape(b.get("text",""))
        header = f'<div class="cls-header" style="background:{bg}"><span class="cls-type" style="color:{fg}">{ctype}</span><span class="cls-label" style="color:{fg}">{clabel}</span></div>'
        parts.append(f'<div class="cls-block" style="border:1px solid {bg};border-radius:6px;overflow:hidden">{header}<div class="cls-text">{text}</div></div>')
    return "".join(parts)

PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Protocol Reader — Labi</title>
<style>{css}</style>
</head>
<body>
<div class="shell">
  <div class="sidebar">
    <div class="sidebar-header">
      <h1>Protocol Reader</h1>
      <p>Top {total} protocols ranked by complexity</p>
    </div>
    <div class="search-wrap">
      <input type="text" id="search" placeholder="Filter..." oninput="filterList(this.value)">
    </div>
    <div class="protocol-list" id="list">{list_html}</div>
  </div>
  <div class="main" id="main">
    <div class="empty">← Select a protocol to view raw vs classified</div>
  </div>
</div>
<script>
function loadProtocol(rank) {{
  document.querySelectorAll('.proto-item').forEach(el => el.classList.remove('active'));
  const item = document.getElementById('item-' + rank);
  if (item) item.classList.add('active');
  document.getElementById('main').innerHTML = '<div class="empty">Loading...</div>';
  fetch('/protocol/' + rank)
    .then(r => r.text())
    .then(h => {{ document.getElementById('main').innerHTML = h; }})
    .catch(e => {{ document.getElementById('main').innerHTML = '<div class="empty" style="color:#f87171">Error: ' + e + '</div>'; }});
}}
function filterList(q) {{
  q = q.toLowerCase();
  document.querySelectorAll('.proto-item').forEach(el => {{
    el.style.display = el.innerText.toLowerCase().includes(q) ? '' : 'none';
  }});
}}
</script>
</body></html>"""

def render_list(ranked):
    items = []
    for p in ranked:
        src = p["source"]
        badge_class = {"pmc":"badge-pmc","protocols_io":"badge-pio"}.get(src,"badge-pmc")
        src_label = {"pmc":"PMC","protocols_io":"protocols.io","zenodo":"Zenodo"}.get(src, src)
        classified_file = CLASSIFIED / f"{p['filename']}.json"
        cls_badge = '<span class="badge classified-badge">✓ classified</span>' if classified_file.exists() else ""
        items.append(f"""
        <div class="proto-item" onclick="loadProtocol({p['rank']})" id="item-{p['rank']}">
          <div class="proto-rank">#{p['rank']}</div>
          <div class="proto-title">{html.escape(p['title'][:80])}{'…' if len(p['title'])>80 else ''}</div>
          <div class="proto-meta">
            <span class="badge {badge_class}">{src_label}</span>
            {cls_badge}
            <span>{p.get('score',0):.0f} pts</span>
          </div>
        </div>""")
    return "\n".join(items)

# ── server ────────────────────────────────────────────────────────────────────

RANKED     = load_analysis()
RANKED_MAP = {p["rank"]: p for p in RANKED}

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args): pass

    def do_GET(self):
        if self.path in ("/", ""):
            self.serve_index()
        elif self.path.startswith("/protocol/"):
            rank = int(self.path.split("/")[-1])
            self.serve_protocol(rank)
        else:
            self.send_error(404)

    def serve_index(self):
        page = PAGE_TEMPLATE.format(
            css=CSS,
            total=len(RANKED),
            list_html=render_list(RANKED),
        )
        self.respond(200, page, "text/html")

    def serve_protocol(self, rank):
        entry = RANKED_MAP.get(rank)
        if not entry:
            self.respond(404, "<div class='empty'>Not found</div>", "text/html")
            return

        source   = entry["source"]
        filename = entry["filename"]
        title    = entry["title"]
        bd       = entry.get("breakdown", {})

        # Load raw blocks
        raw_blocks = []
        if source == "pmc":
            fpath = RAW / "pmc" / filename
            if fpath.exists():
                raw_blocks = raw_blocks_pmc(fpath.read_bytes())
        elif source == "protocols_io":
            fpath = RAW / "protocols_io" / filename
            if fpath.exists():
                try: raw_blocks = raw_blocks_pio(json.loads(fpath.read_bytes()))
                except: pass

        # Load classified data if available
        cls_file = CLASSIFIED / f"{filename}.json"
        classified_data = None
        if cls_file.exists():
            try: classified_data = json.loads(cls_file.read_text())
            except: pass

        src_label = {"pmc":"PMC","protocols_io":"protocols.io","zenodo":"Zenodo"}.get(source, source)
        meta = f"{bd.get('char_count',0):,} chars · {bd.get('num_sections',0)} sections · {bd.get('conditional_logic',0)} conditionals · {len(raw_blocks)} blocks"

        content = f"""
<div class="proto-title-bar">
  <h2>{html.escape(title)}</h2>
  <p>{src_label} · {meta}</p>
</div>
<div class="split">
  <div class="pane">
    <div class="pane-header">Raw original — exactly as received from source</div>
    {render_raw_pane(raw_blocks)}
  </div>
  <div class="pane">
    <div class="pane-header">Classified — what each block is</div>
    {render_classified_pane(classified_data)}
  </div>
</div>"""
        self.respond(200, content, "text/html")

    def respond(self, code, body, ctype):
        enc = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8")
        self.send_header("Content-Length", len(enc))
        self.end_headers()
        self.wfile.write(enc)

if __name__ == "__main__":
    port = 7070
    server = HTTPServer(("", port), Handler)
    print(f"\n  Protocol Reader → http://localhost:{port}\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
