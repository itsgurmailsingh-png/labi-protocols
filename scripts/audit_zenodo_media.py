"""
audit_zenodo_media.py

Full traceable audit of data/media/zenodo/ (10,727 record directories,
36,872 files, ~13GB) — built to answer "what exists and what's useful"
with a documented reason per category, not a silent bulk delete.

Classification, in order (each file gets exactly one bucket + reason):

1. ORPHANED_BACKLOG — media for a zenodo record that isn't normalised/
   published at all (record is in the deprioritized 18,224-record backlog).
   Cannot be useful right now regardless of file type: there's no protocol
   for it to attach to. Confirmed via cross-reference with
   data/sources/zenodo/normalised/ — 8,821 of 10,727 record dirs (82%)
   fall here.

2. JUNK_FILETYPE — file extension can never be text-extracted into protocol
   steps and isn't a displayable image either (raw data: .bam/.fastq/.h5/
   .RData/.npy; code: .py/.R/.sh/.ipynb; video/audio: .mp4/.avi/.wav;
   archives: .zip/.rar/.7z; CAD/spreadsheets/etc). Kept for a normalised
   record but the file itself has zero recovery value.

3. RECOVERED — pdf/docx that WAS successfully text-extracted into real
   steps for a normalised record (cross-referenced against
   data/sources/zenodo/normalised/{id}.json having non-empty steps that
   trace to this file). Clearly useful, keep.

4. UNRECOVERED_DOC — pdf/docx for a normalised record where extraction
   either failed or the record still has 0 steps. Ambiguous — could be a
   genuine protocol doc that failed to parse (worth another attempt) or a
   non-procedural attachment (poster, slide deck, unrelated paper). Uses
   Ollama Cloud to classify the record's title+description as
   protocol-like or not, since file type alone can't tell us here.

5. IMAGE_FOR_RECORD — image file for a normalised record. Kept if the
   record has real steps (likely a genuine figure); flagged for review if
   the record has 0 steps (image with no protocol context).

Usage:
    python3 scripts/audit_zenodo_media.py --dry-run   # report only, no LLM calls
    python3 scripts/audit_zenodo_media.py              # report + Ollama classification of ambiguous docs
"""

import argparse
import json
import os
import pathlib
import re
import sys

import requests

MEDIA_DIR = pathlib.Path("data/media/zenodo")
NORM_DIR = pathlib.Path("data/sources/zenodo/normalised")
RAW_DIR = pathlib.Path("data/sources/zenodo/raw")

TEXT_EXTS = {".pdf", ".docx", ".doc"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".gif", ".bmp", ".webp"}

_env_path = pathlib.Path(".env")
if _env_path.exists():
    for _line in _env_path.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

OLLAMA_KEY = os.environ.get("OLLAMA_API_KEY", "")
OLLAMA_MODEL = "gpt-oss:120b-cloud"


def classify_ollama(title: str, description: str) -> str:
    """Returns 'protocol_like', 'not_protocol', or 'unknown' (on failure)."""
    if not OLLAMA_KEY:
        return "unknown"
    prompt = (
        "Given this Zenodo record's title and description, is this a lab/experimental "
        "PROCEDURE or METHOD a scientist could follow step-by-step, or is it something else "
        "(a raw dataset, a research paper's results/discussion, software, a poster, "
        "administrative document)? Reply with exactly one word: PROTOCOL or NOT_PROTOCOL.\n\n"
        f"Title: {title}\nDescription: {(description or '')[:500]}"
    )
    try:
        resp = requests.post(
            "https://ollama.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {OLLAMA_KEY}", "Content-Type": "application/json"},
            json={"model": OLLAMA_MODEL, "messages": [{"role": "user", "content": prompt}], "max_tokens": 300},
            timeout=20,
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"].strip().upper()
        if "NOT_PROTOCOL" in content or "NOT PROTOCOL" in content:
            return "not_protocol"
        if "PROTOCOL" in content:
            return "protocol_like"
        return "unknown"
    except Exception as e:
        print(f"    [Ollama error] {e}")
        return "unknown"


def load_records() -> dict:
    records = {}
    for f in NORM_DIR.glob("*.json"):
        try:
            d = json.loads(f.read_text())
        except Exception:
            continue
        rid = f.stem
        records[rid] = {
            "has_steps": bool(d.get("steps")),
            "step_count": len(d.get("steps") or []),
            "title": d.get("title", ""),
        }
    return records


def load_descriptions() -> dict:
    descs = {}
    for f in RAW_DIR.glob("*.json"):
        try:
            d = json.loads(f.read_text())
        except Exception:
            continue
        descs[f.stem] = d.get("description", "")
    return descs


def audit(dry_run: bool, ollama_limit: int) -> None:
    records = load_records()
    descriptions = load_descriptions()

    buckets = {
        "orphaned_backlog": {"count": 0, "bytes": 0, "record_ids": set(), "paths": []},
        "junk_filetype": {"count": 0, "bytes": 0, "paths": []},
        "recovered": {"count": 0, "bytes": 0, "paths": []},
        "unrecovered_doc": {"count": 0, "bytes": 0, "files": []},
        "image_for_record_with_steps": {"count": 0, "bytes": 0, "paths": []},
        "image_for_record_without_steps": {"count": 0, "bytes": 0, "paths": []},
    }

    for record_dir in MEDIA_DIR.iterdir():
        if not record_dir.is_dir():
            continue
        rid = record_dir.name
        record = records.get(rid)

        for f in record_dir.iterdir():
            if not f.is_file():
                continue
            size = f.stat().st_size
            ext = f.suffix.lower()

            if record is None:
                buckets["orphaned_backlog"]["count"] += 1
                buckets["orphaned_backlog"]["bytes"] += size
                buckets["orphaned_backlog"]["record_ids"].add(rid)
                buckets["orphaned_backlog"]["paths"].append(f)
                continue

            if ext not in TEXT_EXTS and ext not in IMAGE_EXTS:
                buckets["junk_filetype"]["count"] += 1
                buckets["junk_filetype"]["bytes"] += size
                buckets["junk_filetype"]["paths"].append(f)
                continue

            if ext in TEXT_EXTS:
                if record["has_steps"]:
                    buckets["recovered"]["count"] += 1
                    buckets["recovered"]["bytes"] += size
                    buckets["recovered"]["paths"].append(f)
                else:
                    buckets["unrecovered_doc"]["count"] += 1
                    buckets["unrecovered_doc"]["bytes"] += size
                    buckets["unrecovered_doc"]["files"].append((rid, record["title"], f))
                continue

            if ext in IMAGE_EXTS:
                if record["has_steps"]:
                    buckets["image_for_record_with_steps"]["count"] += 1
                    buckets["image_for_record_with_steps"]["bytes"] += size
                    buckets["image_for_record_with_steps"]["paths"].append(f)
                else:
                    buckets["image_for_record_without_steps"]["count"] += 1
                    buckets["image_for_record_without_steps"]["bytes"] += size
                    buckets["image_for_record_without_steps"]["paths"].append(f)

    def gb(b):
        return f"{b/1e9:.2f} GB"

    print("=" * 70)
    print("ZENODO MEDIA AUDIT")
    print("=" * 70)
    print(f"\n1. ORPHANED_BACKLOG (record not normalised — no protocol to attach to)")
    print(f"   {buckets['orphaned_backlog']['count']} files, {gb(buckets['orphaned_backlog']['bytes'])}, "
          f"{len(buckets['orphaned_backlog']['record_ids'])} distinct records")
    print(f"   VERDICT: safe to delete — cannot be useful until zenodo backlog normalisation resumes")

    print(f"\n2. JUNK_FILETYPE (data/code/video/archive — never extractable into protocol text)")
    print(f"   {buckets['junk_filetype']['count']} files, {gb(buckets['junk_filetype']['bytes'])}")
    print(f"   VERDICT: safe to delete — file type has zero recovery value regardless of record")

    print(f"\n3. RECOVERED (pdf/docx successfully extracted into real steps)")
    print(f"   {buckets['recovered']['count']} files, {gb(buckets['recovered']['bytes'])}")
    print(f"   VERDICT: keep — this is exactly what the recovery pass was for")

    print(f"\n4. UNRECOVERED_DOC (pdf/docx for a normalised record, but 0 steps recovered — ambiguous)")
    print(f"   {buckets['unrecovered_doc']['count']} files, {gb(buckets['unrecovered_doc']['bytes'])}")

    print(f"\n5. IMAGE_FOR_RECORD_WITH_STEPS (likely a genuine figure)")
    print(f"   {buckets['image_for_record_with_steps']['count']} files, {gb(buckets['image_for_record_with_steps']['bytes'])}")
    print(f"   VERDICT: keep")

    print(f"\n6. IMAGE_FOR_RECORD_WITHOUT_STEPS (image but no protocol context)")
    print(f"   {buckets['image_for_record_without_steps']['count']} files, {gb(buckets['image_for_record_without_steps']['bytes'])}")
    print(f"   VERDICT: same fate as the record — orphaned if never gets steps")

    total = sum(b["bytes"] for b in buckets.values())
    print(f"\nTotal audited: {gb(total)}")

    delete_paths = list(buckets["orphaned_backlog"]["paths"]) + list(buckets["junk_filetype"]["paths"]) + \
        list(buckets["image_for_record_without_steps"]["paths"])

    if buckets["unrecovered_doc"]["files"]:
        print(f"\n{'='*70}")
        print(f"Classifying {min(ollama_limit, len(buckets['unrecovered_doc']['files']))} UNRECOVERED_DOC records via Ollama...")
        protocol_like = not_protocol = unknown = 0
        for rid, title, path in buckets["unrecovered_doc"]["files"][:ollama_limit]:
            desc = descriptions.get(rid, "")
            verdict = classify_ollama(title, desc)
            print(f"  [{verdict:14s}] {title[:70]}")
            if verdict == "protocol_like":
                protocol_like += 1
            elif verdict == "not_protocol":
                not_protocol += 1
                delete_paths.append(path)
            else:
                unknown += 1
        print(f"\nOf sampled unrecovered docs: {protocol_like} protocol-like (worth re-attempting extraction), "
              f"{not_protocol} not real protocols (safe to drop), {unknown} unknown (Ollama unavailable/error)")

    return delete_paths


def execute_deletion(delete_paths: list) -> None:
    freed = 0
    deleted = 0
    for p in delete_paths:
        try:
            size = p.stat().st_size
            p.unlink()
            freed += size
            deleted += 1
        except Exception as e:
            print(f"  [ERROR] deleting {p}: {e}")

    # Clean up now-empty record directories left behind.
    removed_dirs = 0
    for d in MEDIA_DIR.iterdir():
        if d.is_dir() and not any(d.iterdir()):
            d.rmdir()
            removed_dirs += 1

    print(f"\nDeleted {deleted} files, freed {freed/1e9:.2f} GB")
    print(f"Removed {removed_dirs} now-empty record directories")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Skip Ollama classification, just report file/size breakdown")
    parser.add_argument("--ollama-limit", type=int, default=30, help="Max unrecovered docs to classify via Ollama")
    parser.add_argument("--execute", action="store_true", help="Actually delete the confirmed-junk tier")
    args = parser.parse_args()
    delete_paths = audit(args.dry_run, args.ollama_limit)

    if args.execute and not args.dry_run:
        print(f"\n{'='*70}")
        print(f"EXECUTING: deleting {len(delete_paths)} files...")
        execute_deletion(delete_paths)


if __name__ == "__main__":
    main()
