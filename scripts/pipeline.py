"""
Labi Protocol Pipeline — full orchestrator.

Runs all layers in order:
  Layer 1  — Fetch raw protocols from each source
  Layer 2  — Normalise raw → canonical Labi schema (per source)
  Layer 3  — Merge all sources + deduplicate (MinHash LSH)
  Layer 4  — Rebuild search_index.json (also run by GitHub Actions CI)

Usage:
    python scripts/pipeline.py                # full pipeline, all sources
    python scripts/pipeline.py --layer 1      # fetch only
    python scripts/pipeline.py --layer 2      # normalise only
    python scripts/pipeline.py --layer 3      # merge+dedup only
    python scripts/pipeline.py --layer 4      # rebuild search index only
    python scripts/pipeline.py --source bio_protocol --layer 1,2
"""

import argparse
import os
import subprocess
import sys
import pathlib

SCRIPTS = pathlib.Path(__file__).parent

LAYER_SOURCES = {
    "bio_protocol": SCRIPTS / "sources" / "fetch_bio_protocol.py",
    "pubmed_central": SCRIPTS / "sources" / "fetch_pubmed_central.py",
}

LAYER2_SCRIPT = SCRIPTS / "normalise.py"
LAYER3_SCRIPT = SCRIPTS / "merge_and_dedup.py"


def run(script: pathlib.Path, extra_env: dict = None) -> int:
    env = {**os.environ}
    if extra_env:
        env.update(extra_env)
    result = subprocess.run([sys.executable, str(script)], env=env)
    return result.returncode


def layer1(sources: list[str]) -> bool:
    ok = True
    for src in sources:
        script = LAYER_SOURCES.get(src)
        if not script or not script.exists():
            print(f"[SKIP] No fetcher script for source: {src}")
            continue
        print(f"\n{'='*60}")
        print(f" Layer 1 — Fetching: {src}")
        print(f"{'='*60}")
        rc = run(script)
        if rc != 0:
            print(f"[WARN] {src} fetcher exited with code {rc}")
            ok = False
    return ok


def layer2(sources: list[str]) -> bool:
    print(f"\n{'='*60}")
    print(f" Layer 2 — Normalising")
    print(f"{'='*60}")
    for src in sources:
        env = {"NORMALISE_SOURCE": src}
        rc = subprocess.run(
            [sys.executable, str(LAYER2_SCRIPT), "--source", src],
            env={**os.environ}
        ).returncode
        if rc != 0:
            print(f"[WARN] Normalise {src} exited with code {rc}")
    return True


def layer3() -> bool:
    print(f"\n{'='*60}")
    print(f" Layer 3 — Merge + Dedup")
    print(f"{'='*60}")
    rc = run(LAYER3_SCRIPT)
    return rc == 0


def layer4() -> bool:
    print(f"\n{'='*60}")
    print(f" Layer 4 — Rebuild search_index.json")
    print(f"{'='*60}")
    import json

    INDEX_KEYS = {
        "protocol_id", "title", "author", "verification_status",
        "category", "license", "license_verified", "source_name",
        "version_count",
    }

    index = {}
    version_ids: dict[str, list[str]] = {}  # parent_pid → [child pids]
    protocols_dir = pathlib.Path("protocols")

    # Two-pass: first collect parents, then attach version lists
    for p in sorted(protocols_dir.glob("*.json")):
        try:
            proto = json.loads(p.read_text())
        except Exception as e:
            print(f"  Skipping {p.name}: {e}")
            continue
        pid = proto.get("protocol_id")
        if not pid:
            continue
        parent_pid = proto.get("parent_protocol_id")
        if parent_pid:
            # Child version — track it under its parent, do NOT index separately
            version_ids.setdefault(parent_pid, []).append(pid)
        else:
            # Parent (canonical) protocol — index it
            entry = {k: proto.get(k) for k in INDEX_KEYS if proto.get(k) is not None}
            index[pid] = entry

    # Attach version id lists to parents
    for parent_pid, vids in version_ids.items():
        if parent_pid in index:
            index[parent_pid]["version_ids"] = vids

    out = pathlib.Path("search_index.json")
    out.write_text(json.dumps(index, separators=(",", ":"), ensure_ascii=False))
    skipped_versions = sum(len(v) for v in version_ids.values())
    print(f"  Index compiled: {len(index)} parent protocols")
    print(f"  Version entries embedded (not indexed separately): {skipped_versions}")
    print(f"  File size: {out.stat().st_size / 1024:.1f} KB")
    return True


def main():
    parser = argparse.ArgumentParser(description="Labi Protocol Pipeline Orchestrator")
    parser.add_argument(
        "--layer",
        default="1,2,3,4",
        help="Comma-separated layers to run (default: 1,2,3,4)"
    )
    parser.add_argument(
        "--source",
        default=",".join(LAYER_SOURCES.keys()),
        help=f"Comma-separated sources (default: all). Options: {', '.join(LAYER_SOURCES.keys())}"
    )
    args = parser.parse_args()

    layers = [int(x.strip()) for x in args.layer.split(",")]
    sources = [x.strip() for x in args.source.split(",")]

    print(f"Pipeline starting: layers={layers}, sources={sources}")
    print(f"GEMINI_API_KEY: {'set' if os.environ.get('GEMINI_API_KEY') else 'NOT SET (LLM disabled)'}")
    print(f"NCBI_API_KEY:   {'set' if os.environ.get('NCBI_API_KEY') else 'NOT SET (3 req/sec limit)'}")

    if 1 in layers:
        layer1(sources)
    if 2 in layers:
        layer2(sources)
    if 3 in layers:
        layer3()
    if 4 in layers:
        layer4()

    print("\n✓ Pipeline complete.")


if __name__ == "__main__":
    main()
