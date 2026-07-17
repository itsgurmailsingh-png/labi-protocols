"""
extract_steps_from_media_pdf.py

General-purpose (any source, not just protocols_io) recovery pass: for any
zero-step protocol that already has a downloaded PDF in its "media" field
(from fetch_zenodo_media.py, fetch_pmc_figures.py, etc.), extract text and
populate steps if it looks like real procedural content. Same technique
that recovered 370 of 767 protocols_io protocols via their document
attachments — this generalizes it to run against whatever PDFs any
source's media fetcher has already pulled down, since those fetchers only
attach media, they never attempt text extraction.

Safe to re-run repeatedly (idempotent — marks "docs_checked" so already-
processed records are skipped, and only touches records that are still
zero-step with no explicit re-processing flag). Intended to be re-run
periodically as background media fetchers (e.g. zenodo, currently at 16%)
continue downloading more PDFs overnight.

Usage:
    python3 scripts/extract_steps_from_media_pdf.py --dry-run
    python3 scripts/extract_steps_from_media_pdf.py
    python3 scripts/extract_steps_from_media_pdf.py --source zenodo
"""

import argparse
import json
import pathlib
import re

try:
    import pypdf
    PDF_AVAILABLE = True
except ImportError:
    PDF_AVAILABLE = False

try:
    import docx as docx_lib
    DOCX_AVAILABLE = True
except ImportError:
    DOCX_AVAILABLE = False

SOURCES_DIR = pathlib.Path("data/sources")


def extract_text_pdf(path: pathlib.Path) -> str:
    reader = pypdf.PdfReader(str(path))
    parts = []
    for page in reader.pages:
        text = page.extract_text() or ""
        if text.strip():
            parts.append(text.strip())
    return "\n\n".join(parts)


def extract_text_docx(path: pathlib.Path) -> str:
    doc = docx_lib.Document(str(path))
    parts = [p.text.strip() for p in doc.paragraphs if p.text.strip()]
    for table in doc.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells if c.text.strip()]
            if cells:
                parts.append(" | ".join(cells))
    return "\n\n".join(parts)


def text_to_steps(text: str) -> list:
    paras = re.split(r"\n{2,}", text)
    result = []
    for p in paras:
        p = re.sub(r"\s{2,}", " ", p.strip())
        if len(p) > 20:
            result.append({
                "title": "", "instruction": p,
                "substeps": [], "is_critical": False, "timers": [],
            })
    return result


def process(source_filter: str | None, dry_run: bool) -> None:
    sources = [source_filter] if source_filter else [d.name for d in SOURCES_DIR.iterdir() if d.is_dir()]

    checked = recovered = skipped_no_media = skipped_already = 0

    for source in sources:
        norm_dir = SOURCES_DIR / source / "normalised"
        if not norm_dir.exists():
            continue

        for norm_path in norm_dir.glob("*.json"):
            try:
                d = json.loads(norm_path.read_text())
            except Exception:
                continue

            if d.get("steps"):
                continue
            if d.get("media_extraction_checked"):
                skipped_already += 1
                continue

            checked += 1
            media = d.get("media") or []
            extractable = [m for m in media if m.get("type") in ("pdf", "document")
                           and pathlib.Path(m.get("local_path", "")).exists()]

            if not extractable:
                skipped_no_media += 1
                if not dry_run:
                    d["media_extraction_checked"] = True
                    norm_path.write_text(json.dumps(d, ensure_ascii=False, indent=2))
                continue

            recovered_steps = None
            for m in extractable:
                path = pathlib.Path(m["local_path"])
                ext = path.suffix.lower().lstrip(".")
                try:
                    if ext == "pdf" and PDF_AVAILABLE:
                        text = extract_text_pdf(path)
                    elif ext == "docx" and DOCX_AVAILABLE:
                        text = extract_text_docx(path)
                    else:
                        continue
                    steps = text_to_steps(text)
                    if len(steps) >= 2:
                        recovered_steps = steps
                        break
                except Exception as e:
                    print(f"    [EXTRACT ERROR] {path}: {e}")

            if dry_run:
                if recovered_steps:
                    print(f"  [WOULD RECOVER] {source}/{norm_path.stem}: {len(recovered_steps)} steps")
                continue

            d["media_extraction_checked"] = True
            if recovered_steps:
                for i, s in enumerate(recovered_steps):
                    s["step_id"] = i
                d["steps"] = recovered_steps
                recovered += 1
                print(f"  [RECOVERED] {source}/{norm_path.stem}: {len(recovered_steps)} steps")

            norm_path.write_text(json.dumps(d, ensure_ascii=False, indent=2))

    print(f"\nChecked: {checked} | Recovered: {recovered} | No extractable media: {skipped_no_media} | Already checked (skipped): {skipped_already}")
    if dry_run:
        print("(dry run — no files written)")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", help="Limit to one source")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    process(args.source, args.dry_run)


if __name__ == "__main__":
    main()
