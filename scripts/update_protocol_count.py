#!/usr/bin/env python3
"""
update_protocol_count.py — patches the live protocol count into protocols.html
Run after any fetch/pipeline step.

Usage:
    python3 scripts/update_protocol_count.py
    python3 scripts/update_protocol_count.py --marketing-dir /path/to/labi-marketing-dev
"""
import pathlib
import re
import argparse

MERGED_DIR  = pathlib.Path(__file__).parent.parent / "protocols"   # final deduped output
SOURCES_DIR = pathlib.Path(__file__).parent.parent / "data" / "sources"
DEFAULT_MARKETING = pathlib.Path(__file__).parent.parent.parent / "labi-marketing-dev"

PROTOCOLS_HTML = "protocols.html"


def count_protocols() -> dict:
    """
    Count from the final merged/deduped protocols/ directory.
    Falls back to raw source counts only if pipeline hasn't been run yet.
    """
    # Final output after Layer 3 merge+dedup — the authoritative number
    # Only use if it has a meaningful number of protocols (>50 = real pipeline run)
    if MERGED_DIR.exists():
        merged_files = list(MERGED_DIR.glob("*.json"))
        if len(merged_files) > 50:
            total = len(merged_files)
            return {"merged (deduplicated)": total, "total": total}

    # Pipeline not run yet — count raw per source (includes duplicates, for info only)
    counts = {}
    total = 0
    for source_dir in SOURCES_DIR.iterdir():
        if not source_dir.is_dir():
            continue
        raw = source_dir / "raw"
        if raw.exists():
            n = len(list(raw.glob("*.json")))
            counts[source_dir.name] = n
            total += n
    counts["total"] = total
    counts["_warning"] = "RAW counts — run Layer 3 (merge_and_dedup.py) first for real number"
    return counts


def round_down_to_nearest_hundred(n: int) -> str:
    """Return human label like '2,600+' rounded down to nearest 100."""
    floored = (n // 100) * 100
    return f"{floored:,}+"


def patch_html(html_path: pathlib.Path, total: int, label: str) -> bool:
    if not html_path.exists():
        print(f"[SKIP] {html_path} not found")
        return False

    original = html_path.read_text(encoding="utf-8")
    updated = original

    # Replace h1 heading
    updated = re.sub(
        r"(>\s*)\d[\d,]+\+(\s*lab protocols)",
        rf"\g<1>{label}\g<2>",
        updated,
    )

    # Replace signal-value span (the big stat pill)
    updated = re.sub(
        r'(<span class="signal-value">)\d[\d,]+\+(<\/span>)',
        rf"\g<1>{label}\g<2>",
        updated,
    )

    # Replace phone preview badge "1,000+ protocols"
    updated = re.sub(
        r'(\d[\d,]+\+\s*protocols)',
        f"{label} protocols",
        updated,
    )

    # Replace meta description count
    updated = re.sub(
        r'\d[\d,]+\+\s*CC-BY',
        f"{label} CC-BY",
        updated,
    )

    if updated == original:
        print(f"[WARN] No replacements made in {html_path.name} — check patterns")
        return False

    html_path.write_text(updated, encoding="utf-8")
    return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--marketing-dir",
        type=pathlib.Path,
        default=DEFAULT_MARKETING,
        help="Path to labi-marketing-dev directory",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print counts without writing files",
    )
    args = parser.parse_args()

    counts = count_protocols()
    total = counts.pop("total")
    warning = counts.pop("_warning", None)
    label = round_down_to_nearest_hundred(total)

    if warning:
        print(f"\n⚠️  {warning}")
    print(f"\nProtocol counts:")
    for src, n in sorted(counts.items()):
        print(f"  {src:30s} {n:>5}")
    print(f"  {'TOTAL':30s} {total:>5}  →  label: {label}")

    if args.dry_run:
        print("\n[dry-run] No files written.")
        return

    html_path = args.marketing_dir / PROTOCOLS_HTML
    ok = patch_html(html_path, total, label)
    if ok:
        print(f"\n✅ Patched {html_path}")
    else:
        print(f"\n❌ Failed to patch {html_path}")


if __name__ == "__main__":
    main()
