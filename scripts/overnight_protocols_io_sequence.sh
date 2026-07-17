#!/bin/bash
# Sequenced overnight pipeline for protocols_io — runs the steps that MUST
# happen in order (each writes to the same normalised/ files as the next
# step reads) rather than racing them like the other 9 sources. No `set -e`
# deliberately — an intermediate failure should not prevent the final
# restructuring step from at least attempting to run overnight.
cd /home/itsgurmailsingh/Documents/Labi_Ecosystem/labi-protocols

echo "[$(date)] Waiting for document recovery (PID 60537) to finish..."
while ps -p 60537 > /dev/null 2>&1; do sleep 10; done
echo "[$(date)] Document recovery finished."

echo "[$(date)] Running materials/background cleanup on newly recovered protocols_io content..."
python3 scripts/fix_broken_steps.py --source protocols_io

echo "[$(date)] Checkpoint: merge + gate..."
python3 scripts/merge_and_dedup.py
python3 scripts/verify_protocol_quality.py --source protocols_io

echo "[$(date)] Resuming protocols_io title/substep restructuring..."
python3 scripts/restructure_steps_llm.py --source protocols_io

echo "[$(date)] protocols_io overnight sequence complete."
