#!/bin/bash
# pipeline_monitor.sh
# Runs every 10 minutes:
# 1. Checks fetch pipeline progress
# 2. Deduplicates raw files
# 3. Normalises any new raw files
# 4. Rebuilds index.json
# 5. Pushes to GitHub (CDN auto-updates)
# 6. Deploys to Vercel

set -e
LOG=/tmp/pipeline_monitor.log
PROTOCOLS_DIR=/home/itsgurmailsingh/Documents/labi-protocols
MARKETING_DIR=/home/itsgurmailsingh/Documents/labi-marketing-next

echo "========================================" | tee -a $LOG
echo "[$(date '+%H:%M:%S')] PIPELINE CHECK" | tee -a $LOG
echo "========================================" | tee -a $LOG

cd $PROTOCOLS_DIR

# ── 1. Fetch progress ──────────────────────────────────────────
echo "" | tee -a $LOG
echo "[1] Fetch pipeline status:" | tee -a $LOG
for src in star_protocols biological_procedures methodsx current_protocols; do
    COUNT=$(ls data/sources/$src/raw/ 2>/dev/null | wc -l)
    NORM=$(ls data/sources/$src/normalised/ 2>/dev/null | wc -l)
    echo "  $src: $COUNT raw | $NORM normalised" | tee -a $LOG
done

# ── 2. Deduplicate raw files (by protocol_id / title hash) ────
echo "" | tee -a $LOG
echo "[2] Deduplication check..." | tee -a $LOG
python3 - <<'PYEOF' 2>&1 | tee -a $LOG
import json, pathlib, hashlib
from collections import defaultdict

SOURCES_DIR = pathlib.Path("data/sources")
sources = ["star_protocols", "biological_procedures", "methodsx", "current_protocols",
           "bio_protocol", "pubmed_central", "github_opentrons", "zenodo"]

seen_titles = defaultdict(list)
removed = 0

for src in sources:
    raw_dir = SOURCES_DIR / src / "raw"
    if not raw_dir.exists():
        continue
    for f in raw_dir.glob("*.json"):
        try:
            d = json.loads(f.read_text())
            title = d.get("title", "").strip().lower()
            if not title:
                continue
            key = hashlib.md5(title.encode()).hexdigest()
            seen_titles[key].append((src, f))
        except:
            pass

for key, entries in seen_titles.items():
    if len(entries) > 1:
        # Keep the first, remove rest
        for src, f in entries[1:]:
            f.unlink()
            removed += 1

print(f"  Duplicates removed: {removed}")
PYEOF

# ── 3. Normalise new raw files ─────────────────────────────────
echo "" | tee -a $LOG
echo "[3] Normalising new files..." | tee -a $LOG
for src in star_protocols biological_procedures methodsx current_protocols; do
    RAW=$(ls data/sources/$src/raw/ 2>/dev/null | wc -l)
    NORM=$(ls data/sources/$src/normalised/ 2>/dev/null | wc -l)
    if [ "$RAW" -gt "$NORM" ]; then
        echo "  Running normaliser for $src ($RAW raw, $NORM normalised)..." | tee -a $LOG
        python3 scripts/normalise.py --source $src >> $LOG 2>&1
    else
        echo "  $src: up to date" | tee -a $LOG
    fi
done

# Also backfill substeps on any new files
echo "  Backfilling sub-steps..." | tee -a $LOG
python3 scripts/add_substeps.py >> $LOG 2>&1

# ── 4. Quality check ───────────────────────────────────────────
echo "" | tee -a $LOG
echo "[4] Quality check (3+ steps only):" | tee -a $LOG
python3 - <<'PYEOF' 2>&1 | tee -a $LOG
import json, pathlib
sources = ["star_protocols","biological_procedures","methodsx","current_protocols",
           "bio_protocol","pubmed_central","github_opentrons","zenodo"]
total_good = 0
for src in sources:
    d = pathlib.Path(f"data/sources/{src}/normalised")
    if not d.exists(): continue
    good = sum(1 for f in d.glob("*.json") if len(json.loads(f.read_text()).get("steps",[])) >= 3)
    total_good += good
    print(f"  {src}: {good} protocols with 3+ steps")
print(f"  TOTAL GOOD: {total_good}")
PYEOF

# ── 5. Rebuild index ───────────────────────────────────────────
echo "" | tee -a $LOG
echo "[5] Rebuilding index.json..." | tee -a $LOG
python3 scripts/build_index.py 2>&1 | tail -3 | tee -a $LOG

# ── 6. Push to GitHub ──────────────────────────────────────────
echo "" | tee -a $LOG
echo "[6] Pushing to GitHub..." | tee -a $LOG
git add index.json
git diff --cached --quiet && echo "  No changes to push." | tee -a $LOG || {
    git commit -m "Auto: rebuild index $(date '+%Y-%m-%d %H:%M')" && git push && echo "  Pushed." | tee -a $LOG
}

# ── 7. Deploy to Vercel ────────────────────────────────────────
echo "" | tee -a $LOG
echo "[7] Deploying to Vercel..." | tee -a $LOG
cd $MARKETING_DIR
npx vercel --prod --yes 2>&1 | grep -E "Aliased|Error|READY" | tee -a $LOG

echo "" | tee -a $LOG
echo "[$(date '+%H:%M:%S')] Done." | tee -a $LOG
