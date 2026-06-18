"""
fetch_github_protocols.py

Fetches open-source lab protocols from GitHub repositories via git clone.

Sources:
  1. Opentrons/Protocols  — Apache 2.0
     https://github.com/Opentrons/Protocols
     Each protocol lives in protocols/{id}/ with README.md + .py file.
     README has: title, author, categories, description, labware, Protocol Steps.

  2. OpenPlant Automation Protocols — MIT + CC0
     https://github.com/openplant/openplant_automation_protocols

Legal basis:
  - Apache 2.0 and MIT explicitly permit redistribution and use in any product.
  - git clone of public repos: no ToS restriction.

Flow:
  1. git clone --depth 1 into /tmp/
  2. Walk protocol folders, parse README.md for rich metadata
  3. Save to data/sources/github_opentrons/raw/{id}.json
  4. Resume-safe: skip already-saved IDs

Usage:
    python3 scripts/sources/fetch_github_protocols.py
    MAX_RECORDS=2000 python3 scripts/sources/fetch_github_protocols.py
"""

import datetime
import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import tempfile

# ── Config ────────────────────────────────────────────────────────────────────
MAX_RECORDS = int(os.environ.get("MAX_RECORDS", "5000"))

RAW_DIR = pathlib.Path("data/sources/github_opentrons/raw")
RAW_DIR.mkdir(parents=True, exist_ok=True)

# (clone_url, license, display_repo, protocols_subdir)
REPOS = [
    (
        "https://github.com/Opentrons/Protocols.git",
        "Apache-2.0",
        "Opentrons/Protocols",
        "protocols",   # subdirectory containing protocol folders
    ),
    (
        "https://github.com/openplant/openplant_automation_protocols.git",
        "MIT",
        "openplant/openplant_automation_protocols",
        ".",           # protocols are at root level
    ),
]


# ── Git clone ─────────────────────────────────────────────────────────────────
def clone_repo(url: str, dest: pathlib.Path) -> bool:
    print(f"  Cloning {url} …")
    result = subprocess.run(
        ["git", "clone", "--depth", "1", "--quiet", url, str(dest)],
        capture_output=True, text=True, timeout=300
    )
    if result.returncode != 0:
        print(f"  Clone failed: {result.stderr[:200]}")
        return False
    print(f"  Cloned OK")
    return True


# ── README parser ─────────────────────────────────────────────────────────────
def _md_section(text: str, header: str) -> str:
    """Extract content of a markdown section by header (case-insensitive)."""
    pattern = rf"(?m)^#{{1,4}}\s+{re.escape(header)}\s*$\n(.*?)(?=^#{{1,4}}\s|\Z)"
    m = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
    return m.group(1).strip() if m else ""


def _md_bullets(text: str) -> list[str]:
    """Extract bullet point text from markdown list."""
    items = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith(("* ", "- ", "+ ")):
            # Strip markdown links [text](url) → text
            item = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", line[2:]).strip()
            if item and len(item) > 2:
                items.append(item)
        elif line.startswith(("\t* ", "\t- ")):
            # Nested bullets (subcategories)
            item = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", line[3:]).strip()
            if item and len(item) > 2:
                items.append(item)
    return items


def _md_numbered(text: str) -> list[str]:
    """Extract numbered list items from markdown."""
    items = []
    for line in text.splitlines():
        m = re.match(r"^\d+\.\s+(.+)", line.strip())
        if m:
            step = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", m.group(1)).strip()
            step = re.sub(r"!\[.*?\]\(.*?\)", "", step).strip()  # remove images
            if step and len(step) > 5:
                items.append(step)
    return items


def parse_readme(readme: str, folder_id: str, display_repo: str, license_id: str) -> dict | None:
    """Parse Opentrons README.md into raw schema."""

    # Title: first H1
    title_m = re.match(r"#\s+(.+)", readme.strip())
    title = title_m.group(1).strip() if title_m else ""
    # Clean up title
    title = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", title).strip()
    if not title or len(title) < 3:
        return None

    # Author
    author_sec = _md_section(readme, "Author")
    author = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", author_sec).strip()
    author = re.sub(r"\s+", " ", author)[:100]

    # Description
    description = _md_section(readme, "Description")
    description = re.sub(r"!\[.*?\]\(.*?\)", "", description).strip()
    description = re.sub(r"\s+", " ", description)[:2000]

    # Category from Categories section
    cat_sec = _md_section(readme, "Categories")
    cat_bullets = _md_bullets(cat_sec)
    category = cat_bullets[0] if cat_bullets else ""

    # Materials: Labware + Pipettes + Reagents
    labware    = _md_bullets(_md_section(readme, "Labware"))
    pipettes   = _md_bullets(_md_section(readme, "Pipettes"))
    reagents   = _md_bullets(_md_section(readme, "Reagent Setup") or _md_section(readme, "Reagents"))
    materials  = labware + pipettes + reagents

    # Steps: "Protocol Steps" section (actual numbered procedure)
    steps_sec = (
        _md_section(readme, "Protocol Steps")
        or _md_section(readme, "Steps")
        or _md_section(readme, "Process")
    )
    steps_numbered = _md_numbered(steps_sec)

    # steps_raw: description as first item + numbered steps
    steps_raw: list = []
    if description:
        steps_raw.append(description)
    steps_raw.extend(steps_numbered)

    source_id  = hashlib.md5(f"{display_repo}/{folder_id}".encode()).hexdigest()[:12]
    source_url = f"https://github.com/{display_repo}/tree/main/{folder_id}" \
                 if "Opentrons" in display_repo else f"https://github.com/{display_repo}"

    return {
        "source_name":      "github_opentrons",
        "source_id":        source_id,
        "source_url":       source_url,
        "doi":              "",
        "title":            title,
        "author":           author,
        "license":          license_id,
        "license_verified": True,
        "license_note":     f"GitHub repo {display_repo} — {license_id}",
        "description":      description,
        "steps_raw":        steps_raw,
        "keywords":         cat_bullets,
        "materials_raw":    materials,
        "resource_type":    "protocol",
        "fetched_at":       datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }


# ── Fetch ─────────────────────────────────────────────────────────────────────
def fetch_repo(clone_url: str, license_id: str, display_repo: str,
               protocols_subdir: str, saved: set, total: list) -> None:
    print(f"\n{'='*60}")
    print(f" {display_repo}  [{license_id}]")
    print(f"{'='*60}")

    with tempfile.TemporaryDirectory() as tmpdir:
        clone_dest = pathlib.Path(tmpdir) / "repo"
        if not clone_repo(clone_url, clone_dest):
            return

        protocols_root = clone_dest / protocols_subdir

        if not protocols_root.exists():
            # Try root directly
            protocols_root = clone_dest

        # Collect README.md files — each README = one protocol
        readmes = list(protocols_root.rglob("README.md"))
        print(f"  Found {len(readmes)} README.md files")

        for readme_path in sorted(readmes):
            if len(total) >= MAX_RECORDS:
                return

            # folder_id: path relative to protocols_root, without README.md
            folder_id = str(readme_path.parent.relative_to(clone_dest))

            source_id = hashlib.md5(f"{display_repo}/{folder_id}".encode()).hexdigest()[:12]
            if source_id in saved:
                continue

            try:
                readme_text = readme_path.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue

            record = parse_readme(readme_text, folder_id, display_repo, license_id)
            if not record:
                print(f"  [SKIP] {folder_id} — no parseable title")
                continue

            out_path = RAW_DIR / f"{source_id}.json"
            out_path.write_text(json.dumps(record, indent=2, ensure_ascii=False))
            saved.add(source_id)
            total.append(source_id)
            print(f"  [{len(total):>4}] {record['title'][:70]}")


def main():
    # Clear old raw records first (they were from .py-only parsing, no steps)
    existing = list(RAW_DIR.glob("*.json"))
    if existing:
        print(f"Clearing {len(existing)} old raw records (re-parsing with README)…")
        for f in existing:
            f.unlink()

    saved: set = set()
    total: list = []

    for clone_url, license_id, display_repo, protocols_subdir in REPOS:
        if len(total) >= MAX_RECORDS:
            break
        fetch_repo(clone_url, license_id, display_repo, protocols_subdir, saved, total)

    print(f"\nDone. New records saved: {len(total)}")
    print(f"Total in raw dir: {len(list(RAW_DIR.glob('*.json')))}")


if __name__ == "__main__":
    main()
