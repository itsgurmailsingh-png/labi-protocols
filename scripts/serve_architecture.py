"""
serve_architecture.py — Live architecture dashboard server.
Reads real pipeline stats on every request and injects them into the HTML.

Usage:
    python3 scripts/serve_architecture.py
    Open: http://localhost:7456
"""

import json
import pathlib
import re
from http.server import BaseHTTPRequestHandler, HTTPServer

REPO       = pathlib.Path(__file__).parent.parent
SOURCES    = REPO / "data" / "sources"
MERGED     = REPO / "data" / "merged"
PROTOCOLS  = REPO / "protocols"
HTML_TMPL  = REPO / "docs" / "pipeline_architecture.html"
PORT       = 7456


def get_stats() -> dict:
    stats = {}

    # protocols_io raw
    pio_raw = SOURCES / "protocols_io" / "raw"
    if pio_raw.exists():
        files = list(pio_raw.glob("*.json"))
        stats["pio_raw"] = len(files)
        has_steps = 0
        has_stats = 0
        for f in files:
            try:
                d = json.loads(f.read_text())
                if d.get("steps_raw"):
                    has_steps += 1
                if d.get("stats") is not None:
                    has_stats += 1
            except Exception:
                pass
        stats["pio_steps"] = has_steps
        stats["pio_stats"] = has_stats
        stats["pio_pct"] = round(100 * has_steps / len(files)) if files else 0
    else:
        stats.update({"pio_raw": 0, "pio_steps": 0, "pio_stats": 0, "pio_pct": 0})

    # PMC raw
    pmc_raw = SOURCES / "pubmed_central" / "raw"
    stats["pmc_raw"] = len(list(pmc_raw.glob("*.json"))) if pmc_raw.exists() else 0

    # Normalised — per source and total
    norm_total = 0
    for src in SOURCES.iterdir():
        nd = src / "normalised"
        if nd.exists():
            n = len(list(nd.glob("*.json")))
            norm_total += n
            stats[f"normalised_{src.name}"] = n
    stats["normalised"] = norm_total
    stats["pio_normalised"] = stats.get("normalised_protocols_io", 0)

    # Merged / version-grouped
    stats["merged"] = len(list(MERGED.glob("*.json"))) if MERGED.exists() else 0

    # CDN-ready (protocols/)
    stats["cdn_ready"] = len(list(PROTOCOLS.glob("*.json"))) if PROTOCOLS.exists() else 0

    # Total raw
    stats["total_raw"] = stats["pio_raw"] + stats["pmc_raw"]

    return stats


def build_html(stats: dict) -> str:
    html = HTML_TMPL.read_text()

    # Inject auto-refresh
    html = html.replace("</head>", '<meta http-equiv="refresh" content="30">\n</head>')

    # Inject live stats banner at top of pipeline div
    live_banner = f"""
  <!-- LIVE STATS — auto-injected by serve_architecture.py -->
  <div style="background:#0d2818; border:1.5px solid #2ea043; border-radius:10px; padding:14px 24px;
              display:flex; gap:24px; align-items:center; justify-content:center; flex-wrap:wrap; margin-bottom:20px;">
    <div style="font-size:11px; color:#3fb950; font-weight:700; letter-spacing:1px; text-transform:uppercase;">
      ⚡ LIVE — refreshes every 30s
    </div>
    <div style="width:1px; height:32px; background:#2ea043;"></div>
    <div style="text-align:center;">
      <div style="font-size:22px; font-weight:700; color:#fff;">{stats['pio_raw']:,}</div>
      <div style="font-size:11px; color:#8b949e;">protocols.io raw</div>
    </div>
    <div style="text-align:center;">
      <div style="font-size:22px; font-weight:700; color:#58a6ff;">{stats['pmc_raw']:,}</div>
      <div style="font-size:11px; color:#8b949e;">PMC raw</div>
    </div>
    <div style="text-align:center;">
      <div style="font-size:22px; font-weight:700; color:#d29922;">{stats['pio_steps']:,} / {stats['pio_raw']:,}</div>
      <div style="font-size:11px; color:#8b949e;">steps backfilled ({stats['pio_pct']}%)</div>
    </div>
    <div style="text-align:center;">
      <div style="font-size:22px; font-weight:700; color:#{'3fb950' if stats['pio_normalised'] > 0 else '8b949e'}">{stats['pio_normalised']:,} / {stats['pio_raw']:,}</div>
      <div style="font-size:11px; color:#8b949e;">protocols.io normalised</div>
    </div>
    <div style="text-align:center;">
      <div style="font-size:22px; font-weight:700; color:#{'3fb950' if stats['merged'] > 50 else '8b949e'}">{stats['merged']:,}</div>
      <div style="font-size:11px; color:#8b949e;">version-grouped</div>
    </div>
    <div style="text-align:center;">
      <div style="font-size:22px; font-weight:700; color:#{'3fb950' if stats['cdn_ready'] > 50 else '8b949e'}">{stats['cdn_ready']:,}</div>
      <div style="font-size:11px; color:#8b949e;">CDN-ready</div>
    </div>
  </div>
"""
    html = html.replace('<div class="pipeline">', '<div class="pipeline">\n' + live_banner)

    # Update progress bar for backfill
    html = re.sub(
        r'(Steps Backfill.*?)<div class="stat">.*?</div>.*?<div class="progress-bar"><div class="fill" style="width:\d+%">',
        lambda m: m.group(0),
        html,
        flags=re.DOTALL
    )

    # Update middle row stat numbers
    html = re.sub(r'id="stat-backfill">[^<]*', f'id="stat-backfill">{stats["pio_pct"]}%', html)
    html = re.sub(r'id="stat-normalised">[^<]*', f'id="stat-normalised">{stats["normalised"]:,}', html)
    html = re.sub(r'id="stat-merged">[^<]*', f'id="stat-merged">{stats["merged"]:,}', html)
    html = re.sub(r'id="stat-cdn">[^<]*', f'id="stat-cdn">{stats["cdn_ready"]:,}', html)

    # Update the backfill percentage text
    html = re.sub(
        r'~24% <span>complete</span>',
        f'{stats["pio_pct"]}% <span>({stats["pio_steps"]:,} / {stats["pio_raw"]:,})</span>',
        html
    )
    html = re.sub(
        r'width:24%',
        f'width:{stats["pio_pct"]}%',
        html
    )

    # Update layer status tags based on real state
    backfill_done = stats["pio_pct"] >= 87 and stats["pio_steps"] > 5000
    normalise_done = stats["normalised"] >= stats["pio_raw"] * 0.9
    normalise_running = 0 < stats["normalised"] < stats["pio_raw"] * 0.9

    # Layer 1b: backfill
    if backfill_done:
        html = html.replace(
            '<span class="tag-running">Running</span>',
            '<span style="display:inline-block;font-size:10px;font-weight:700;padding:2px 8px;border-radius:20px;background:#0d2818;color:#3fb950;letter-spacing:1px;text-transform:uppercase;">✓ Done</span>'
        )

    # Layer 2: normalise
    if normalise_running:
        html = html.replace(
            '<span class="tag-waiting">Waiting for backfill</span>',
            f'<span class="tag-running">Running ({stats["normalised"]:,} / {stats["pio_raw"]:,})</span>'
        )
    elif normalise_done:
        html = html.replace(
            '<span class="tag-waiting">Waiting for backfill</span>',
            '<span style="display:inline-block;font-size:10px;font-weight:700;padding:2px 8px;border-radius:20px;background:#0d2818;color:#3fb950;letter-spacing:1px;text-transform:uppercase;">✓ Done</span>'
        )

    # Layer 3: merge
    if stats["merged"] > 50:
        html = html.replace(
            '<span class="tag-waiting">Waiting</span>',
            '<span style="display:inline-block;font-size:10px;font-weight:700;padding:2px 8px;border-radius:20px;background:#0d2818;color:#3fb950;letter-spacing:1px;text-transform:uppercase;">✓ Done</span>',
            1
        )

    # Footer timestamp
    from datetime import datetime
    ts = datetime.now().strftime("%b %d %Y %H:%M")
    html = re.sub(
        r'Last updated:.*?protocols',
        f'Last updated: {ts} · {stats["total_raw"]:,} CC-BY verified raw protocols',
        html
    )

    return html


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/", "/index.html"):
            try:
                stats = get_stats()
                html  = build_html(stats).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(html)))
                self.end_headers()
                self.wfile.write(html)
            except Exception as e:
                self.send_error(500, str(e))
        elif self.path == "/stats.json":
            data = json.dumps(get_stats(), indent=2).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(data)
        else:
            self.send_error(404)

    def log_message(self, fmt, *args):
        pass  # suppress request logs


if __name__ == "__main__":
    print(f"Architecture dashboard → http://localhost:{PORT}")
    print("Auto-refreshes every 30s. Ctrl+C to stop.\n")
    HTTPServer(("", PORT), Handler).serve_forever()
