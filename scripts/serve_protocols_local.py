"""
serve_protocols_local.py — Local dev server for testing protocol JSON in the Flutter app.

Serves CDN-ready protocols from protocols/ with CORS headers.

Usage:
    python3 scripts/serve_protocols_local.py
    Flutter connects to http://localhost:8765

Endpoints:
    GET /protocols          → JSON array of all protocols (id + title + category only)
    GET /protocols/<id>     → Full protocol JSON
    GET /protocols/sample   → Returns first 5 protocols (for quick testing)
"""

import json
import pathlib
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse

REPO      = pathlib.Path(__file__).parent.parent
PROTOCOLS = REPO / "protocols"
PORT      = 8765


def cors_headers(handler):
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
    handler.send_header("Access-Control-Allow-Headers", "Content-Type")


def send_json(handler, data, status=200):
    body = json.dumps(data, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    cors_headers(handler)
    handler.end_headers()
    handler.wfile.write(body)


def load_protocol(path: pathlib.Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


class Handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self.send_response(204)
        cors_headers(self)
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path   = parsed.path.rstrip("/")

        if not PROTOCOLS.exists():
            send_json(self, {"error": "protocols/ directory not found — run the pipeline first"}, 404)
            return

        files = sorted(PROTOCOLS.glob("*.json"))

        if path == "/protocols/sample":
            # First 5 with steps for quick testing
            result = []
            for f in files:
                if len(result) >= 5:
                    break
                try:
                    d = load_protocol(f)
                    if d.get("steps"):  # only ones with steps
                        result.append(d)
                except Exception:
                    pass
            send_json(self, result)

        elif path == "/protocols":
            # Index — id + title + category + estimated_time_mins only
            index = []
            for f in files:
                try:
                    d = load_protocol(f)
                    index.append({
                        "protocol_id":        d.get("protocol_id", f.stem),
                        "title":              d.get("title", ""),
                        "category":           d.get("category", "Other"),
                        "estimated_time_mins": d.get("estimated_time_mins"),
                        "version_count":      d.get("version_count"),
                        "llm_normalised":     d.get("llm_normalised", False),
                    })
                except Exception:
                    pass
            send_json(self, index)

        elif path.startswith("/protocols/"):
            proto_id = path[len("/protocols/"):]
            # Try exact filename match first
            candidate = PROTOCOLS / f"{proto_id}.json"
            if not candidate.exists():
                # fuzzy: find first file whose protocol_id matches
                candidate = None
                for f in files:
                    try:
                        d = load_protocol(f)
                        if d.get("protocol_id") == proto_id:
                            candidate = f
                            break
                    except Exception:
                        pass
            if candidate and candidate.exists():
                send_json(self, load_protocol(candidate))
            else:
                send_json(self, {"error": f"Protocol '{proto_id}' not found"}, 404)

        else:
            send_json(self, {"error": "Not found"}, 404)

    def log_message(self, fmt, *args):
        print(f"  {args[0]}  {args[1]}  {self.path}")


if __name__ == "__main__":
    count = len(list(PROTOCOLS.glob("*.json"))) if PROTOCOLS.exists() else 0
    print(f"Labi Protocol Dev Server")
    print(f"  Protocols dir : {PROTOCOLS}")
    print(f"  Protocol count: {count}")
    print(f"  Listening on  : http://localhost:{PORT}")
    print(f"  Sample (5)    : http://localhost:{PORT}/protocols/sample")
    print(f"  All index     : http://localhost:{PORT}/protocols")
    print(f"  Flutter uses  : http://10.0.2.2:{PORT}  (Android emulator)")
    print(f"                  http://localhost:{PORT}  (iOS sim / web)")
    print()
    HTTPServer(("", PORT), Handler).serve_forever()
