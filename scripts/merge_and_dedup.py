"""
Layer 3: Merge and deduplicate normalised protocols from multiple sources.

Strategy:
  - Jaccard >= 0.95  → true duplicate (identical content, different source) → keep best
  - Jaccard 0.70–0.95 → same protocol family → group as versions, link via parent_protocol_id
  - Jaccard < 0.70   → unrelated → keep both as independent protocols

Quality score per protocol:
  (runs × 3) + (peer_reviewed × 50) + bookmarks + (views / 100)

The highest-scoring protocol in a version group becomes the parent.
All others set parent_protocol_id → parent's protocol_id.

Output fields include:
  - doi, source_url, author  → full provenance/attribution
  - stats, peer_reviewed     → quality signals
  - quality_score            → computed ranking score
  - parent_protocol_id       → None if parent, else points to parent
  - version_count            → total versions in this family (on parent only)
"""

import json
import os
import re
import shutil
from datetime import datetime, date
from pathlib import Path
from collections import defaultdict

from datasketch import MinHash, MinHashLSH

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
SOURCES_DIR       = Path("data/sources")
MERGED_DIR        = Path("data/merged")
PROTOCOLS_DIR     = Path("protocols")

TRUE_DUP_THRESHOLD    = float(os.environ.get("TRUE_DUP_THRESHOLD", "0.95"))
VERSION_THRESHOLD     = float(os.environ.get("VERSION_THRESHOLD",  "0.70"))
NUM_PERM              = 128

# Higher index = lower priority in dedup. CC-BY sources ranked above CC-BY-SA.
# openwetware is CC-BY-SA 3.0 (share-alike), ranked lowest.
SOURCE_PRIORITY = [
    "bio_protocol",
    "pubmed_central",
    "zenodo",
    "figshare",
    "vendor",
    "protocols_io",
    "openwetware",   # CC-BY-SA 3.0 — lowest priority (share-alike)
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def tokenize(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def quality_score(protocol: dict) -> float:
    stats = protocol.get("stats") or {}
    runs      = int(stats.get("runs", 0))
    bookmarks = int(stats.get("bookmarks", 0))
    views     = int(stats.get("views", 0))
    peer      = 50 if protocol.get("peer_reviewed") else 0
    steps     = len(protocol.get("steps") or protocol.get("steps_raw") or [])
    return (runs * 3) + peer + bookmarks + (views / 100) + (steps * 0.5)


def make_minhash(protocol: dict) -> MinHash:
    title = protocol.get("title", "")
    steps = protocol.get("steps") or []
    steps_raw = protocol.get("steps_raw") or []

    if steps:
        step_text = " ".join(
            s.get("instruction", "") if isinstance(s, dict) else str(s)
            for s in steps
        )
    else:
        step_text = " ".join(str(s) for s in steps_raw)

    combined = (title + " " + step_text)[:2000]
    tokens   = tokenize(combined)

    m = MinHash(num_perm=NUM_PERM)
    for token in tokens:
        m.update(token.encode("utf-8"))
    return m


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return slug[:80] or "protocol"


def source_rank(source_name: str) -> int:
    try:
        return SOURCE_PRIORITY.index(source_name)
    except ValueError:
        return len(SOURCE_PRIORITY)


def build_output(protocol: dict, parent_id: str | None, version_count: int) -> dict:
    """Build final output record with full provenance."""
    steps_raw = protocol.get("steps_raw") or []
    steps = protocol.get("steps") or []

    if not steps and steps_raw:
        steps = [
            {
                "step_id": i + 1,
                "title": s.get("title", "") if isinstance(s, dict) else "",
                "instruction": s.get("instruction", str(s)) if isinstance(s, dict) else str(s),
                "is_critical": False,
                "timers": [],
            }
            for i, s in enumerate(steps_raw)
        ]

    pid = protocol.get("protocol_id") or slugify(protocol.get("title", "unknown"))

    return {
        "protocol_id":        pid,
        "parent_protocol_id": parent_id,
        "version_count":      version_count if parent_id is None else None,
        "title":              protocol.get("title", ""),
        "author":             protocol.get("author", ""),
        "doi":                protocol.get("doi", ""),
        "source_name":        protocol.get("source_name", ""),
        "source_url":         protocol.get("source_url", ""),
        "citation":           protocol.get("doi", ""),
        "license":            protocol.get("license", ""),
        "license_verified":   protocol.get("license_verified", False),
        "license_url":        protocol.get("license_url", ""),
        "license_note":       protocol.get("license_note", ""),
        "peer_reviewed":      bool(protocol.get("peer_reviewed", False)),
        "stats":              protocol.get("stats") or {"views": 0, "runs": 0, "bookmarks": 0, "comments": 0},
        "quality_score":      round(quality_score(protocol), 2),
        "category":           protocol.get("category", "Other"),
        "estimated_time_mins":protocol.get("estimated_time_mins"),
        "materials":          protocol.get("materials") or [],
        "steps":              steps,
        "verification_status":protocol.get("verification_status", "verified" if protocol.get("license_verified") else "unverified"),
        "merged_at":          date.today().isoformat(),
    }


# ---------------------------------------------------------------------------
# Load
# ---------------------------------------------------------------------------

def load_all_protocols() -> tuple[list[dict], dict[str, int]]:
    protocols = []
    counts: dict[str, int] = {}

    for source_dir in sorted(SOURCES_DIR.iterdir()):
        if not source_dir.is_dir():
            continue
        normalised_dir = source_dir / "normalised"
        if not normalised_dir.exists():
            continue
        source_name  = source_dir.name
        source_count = 0
        for json_file in sorted(normalised_dir.glob("*.json")):
            try:
                data = json.loads(json_file.read_text())
                if isinstance(data, list):
                    for item in data:
                        item.setdefault("source_name", source_name)
                        protocols.append(item)
                        source_count += 1
                else:
                    data.setdefault("source_name", source_name)
                    protocols.append(data)
                    source_count += 1
            except Exception as e:
                print(f"  [WARN] Could not load {json_file}: {e}")
        counts[source_name] = source_count

    return protocols, counts


# ---------------------------------------------------------------------------
# Group into version families
# ---------------------------------------------------------------------------

def group_protocols(protocols: list[dict]) -> tuple[list[list[dict]], int, int]:
    """
    Returns:
      - groups: list of version groups (each group = list of protocols)
      - true_dups_removed: count of exact duplicates dropped
      - version_groups: count of groups with >1 member
    """
    # Build MinHashes
    minhashes = [make_minhash(p) for p in protocols]

    # Pass 1 — find true duplicates (>= TRUE_DUP_THRESHOLD)
    lsh_dup = MinHashLSH(threshold=TRUE_DUP_THRESHOLD, num_perm=NUM_PERM)
    deduped_indices = []   # indices that survive true-dup removal
    dup_of: dict[int, int] = {}  # loser → winner

    for i, (p, m) in enumerate(zip(protocols, minhashes)):
        key = f"p_{i}"
        matches = lsh_dup.query(m)
        if matches:
            # True duplicate — pick winner by quality score, then source priority
            winner_idx = int(matches[0].split("_")[1])
            winner = protocols[winner_idx]
            if quality_score(p) > quality_score(winner):
                # New one is better — replace
                lsh_dup.remove(f"p_{winner_idx}")
                lsh_dup.insert(key, m)
                deduped_indices.remove(winner_idx)
                deduped_indices.append(i)
                dup_of[winner_idx] = i
                print(f"  [TRUE DUP] Replaced '{winner.get('title','')[:50]}' with better version '{p.get('title','')[:50]}'")
            else:
                dup_of[i] = winner_idx
                print(f"  [TRUE DUP] Dropped  '{p.get('title','')[:50]}' (src={p.get('source_name')}) — kept existing")
        else:
            lsh_dup.insert(key, m)
            deduped_indices.append(i)

    true_dups_removed = len(protocols) - len(deduped_indices)
    survivors = [(i, protocols[i], minhashes[i]) for i in deduped_indices]

    # Pass 2 — group version families (VERSION_THRESHOLD to TRUE_DUP_THRESHOLD)
    lsh_ver = MinHashLSH(threshold=VERSION_THRESHOLD, num_perm=NUM_PERM)
    group_of: dict[int, int] = {}   # idx → group_leader_idx
    group_members: dict[int, list[int]] = {}  # leader → [member idxs]

    for i, p, m in survivors:
        key = f"p_{i}"
        matches = lsh_ver.query(m)

        if matches:
            # Join existing version family
            leader_idx = int(matches[0].split("_")[1])
            # Walk to root — stop when node is its own leader (self-reference)
            while group_of.get(leader_idx, leader_idx) != leader_idx:
                leader_idx = group_of[leader_idx]
            group_of[i] = leader_idx
            group_members[leader_idx].append(i)
        else:
            lsh_ver.insert(key, m)
            group_of[i] = i  # is its own leader (self-reference = root)
            group_members[i] = [i]

    # Build final groups
    groups = []
    for leader_idx, member_idxs in group_members.items():
        group = [protocols[idx] for idx in member_idxs]
        # Sort by quality score descending — best version first
        group.sort(key=quality_score, reverse=True)
        groups.append(group)

    version_groups = sum(1 for g in groups if len(g) > 1)
    return groups, true_dups_removed, version_groups


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------

def write_outputs(groups: list[list[dict]]) -> int:
    # Clean output dirs first to prevent stale files from previous runs accumulating
    if MERGED_DIR.exists():
        shutil.rmtree(MERGED_DIR)
    MERGED_DIR.mkdir(parents=True, exist_ok=True)
    if PROTOCOLS_DIR.exists():
        shutil.rmtree(PROTOCOLS_DIR)
    PROTOCOLS_DIR.mkdir(parents=True, exist_ok=True)

    written = 0
    for group in groups:
        parent = group[0]
        version_count = len(group)
        parent_pid = parent.get("protocol_id") or slugify(parent.get("title", "unknown"))

        for i, protocol in enumerate(group):
            is_parent = (i == 0)
            out = build_output(
                protocol,
                parent_id     = None if is_parent else parent_pid,
                version_count = version_count if is_parent else None,
            )

            pid = out["protocol_id"]
            # Append version suffix for non-parents to avoid filename collision
            if not is_parent:
                src = re.sub(r"[^a-z0-9]", "", protocol.get("source_name", ""))[:8]
                pid = f"{parent_pid}_v{i}_{src}"
                out["protocol_id"] = pid

            path = MERGED_DIR / f"{pid}.json"
            path.write_text(json.dumps(out, ensure_ascii=False, indent=2))
            shutil.copy2(path, PROTOCOLS_DIR / f"{pid}.json")
            written += 1

    return written


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    print("=== merge_and_dedup.py — Layer 3 ===")
    print(f"True-dup threshold : {TRUE_DUP_THRESHOLD}")
    print(f"Version threshold  : {VERSION_THRESHOLD}")
    print(f"NumPerm            : {NUM_PERM}")

    protocols, counts = load_all_protocols()
    print("\nLoaded per source:")
    for src, n in counts.items():
        print(f"  {src}: {n}")
    print(f"  TOTAL: {len(protocols)}")

    print("\nGrouping protocols into version families...")
    groups, true_dups, ver_groups = group_protocols(protocols)

    print("\nWriting outputs...")
    written = write_outputs(groups)

    print("\n=== Statistics ===")
    for src, n in counts.items():
        print(f"  {src}: {n} loaded")
    print(f"  True duplicates removed : {true_dups}")
    print(f"  Version families        : {ver_groups}")
    print(f"  Unique protocol files   : {written}")
    print(f"  Written to              : {MERGED_DIR}/ and {PROTOCOLS_DIR}/")


if __name__ == "__main__":
    main()
