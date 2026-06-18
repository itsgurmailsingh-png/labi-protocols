"""
pipeline_runner.py — Automated pipeline: normalise → fill_step_titles → merge → done.

Monitors progress, runs quality checks every 100 files, emails on issues or completion.

Usage:
    python3 scripts/pipeline_runner.py

Email config (add to .env):
    NOTIFY_EMAIL=itsgurmailsingh@gmail.com
    SMTP_USER=itsgurmailsingh@gmail.com
    SMTP_PASS=your_gmail_app_password   # https://myaccount.google.com/apppasswords
"""

import json
import os
import re
import sys
import time
import pathlib
import smtplib
import subprocess
from email.mime.text import MIMEText
from datetime import datetime

# ── Config ────────────────────────────────────────────────────────────────────
NOTIFY_EMAIL = os.environ.get("NOTIFY_EMAIL", "itsgurmailsingh@gmail.com")
SMTP_USER    = os.environ.get("SMTP_USER", "")
SMTP_PASS    = os.environ.get("SMTP_PASS", "")
SOURCES_DIR  = pathlib.Path("data/sources/protocols_io")
POLL_SECS    = 120   # check every 2 minutes
CHECK_EVERY  = 100   # quality check every N new files

# ── Email ─────────────────────────────────────────────────────────────────────

def send_email(subject: str, body: str):
    if not SMTP_USER or not SMTP_PASS:
        print(f"\n[EMAIL SKIPPED — no SMTP creds]\nSubject: {subject}\n{body}\n")
        return
    try:
        msg = MIMEText(body)
        msg["Subject"] = f"[Labi Pipeline] {subject}"
        msg["From"]    = SMTP_USER
        msg["To"]      = NOTIFY_EMAIL
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(SMTP_USER, SMTP_PASS)
            s.sendmail(SMTP_USER, NOTIFY_EMAIL, msg.as_string())
        print(f"  [email sent] {subject}")
    except Exception as e:
        print(f"  [email failed] {e}")


# ── Quality check ─────────────────────────────────────────────────────────────

def run_quality_check() -> dict:
    nd = SOURCES_DIR / "normalised"
    stats = {"total": 0, "clean": 0, "corrupted": 0, "junk": 0, "no_steps": 0}

    for f in nd.glob("*.json"):
        stats["total"] += 1
        try:
            d = json.loads(f.read_text())
        except Exception:
            stats["corrupted"] += 1
            continue

        steps = d.get("steps", [])
        if not steps:
            stats["no_steps"] += 1
            continue

        first_instr = steps[0].get("instruction", "")
        if first_instr.startswith("{'title'") or first_instr.startswith('{"title"'):
            stats["corrupted"] += 1
        elif not re.search(r"[a-zA-Z]{3,}", first_instr) or len(first_instr) < 10:
            stats["junk"] += 1
        else:
            stats["clean"] += 1

    return stats


def quality_is_bad(stats: dict) -> bool:
    if stats["total"] == 0:
        return False
    corrupt_pct = (stats["corrupted"] + stats["junk"]) / stats["total"] * 100
    return corrupt_pct > 5  # alert if >5% bad


# ── Step runners ──────────────────────────────────────────────────────────────

def run_step(cmd: list, log_file: str, label: str) -> bool:
    print(f"\n{'='*60}")
    print(f"  STARTING: {label}")
    print(f"  Log: {log_file}")
    print(f"{'='*60}")

    with open(log_file, "w") as log:
        proc = subprocess.Popen(cmd, stdout=log, stderr=log)

    last_check = 0
    last_count = 0

    while proc.poll() is None:
        time.sleep(POLL_SECS)
        count = len(list((SOURCES_DIR / "normalised").glob("*.json")))
        print(f"  [{datetime.now().strftime('%H:%M')}] {label}: {count} files done")

        # Quality check every CHECK_EVERY new files
        if count - last_check >= CHECK_EVERY and count > last_check:
            stats = run_quality_check()
            print(f"  Quality: {stats['clean']} clean | {stats['corrupted']} corrupted | {stats['junk']} junk")
            if quality_is_bad(stats):
                # CIRCUIT BREAKER — kill the process, don't let it keep writing bad data
                print(f"  [CIRCUIT BREAKER] Quality too bad — killing process")
                proc.kill()
                send_email(
                    "PIPELINE STOPPED — Data corruption detected, process killed",
                    f"Quality check failed at {count} files — process was killed automatically.\n\n"
                    f"Stats: {json.dumps(stats, indent=2)}\n\n"
                    f"Log: {log_file}\n\n"
                    f"Check the log, fix the issue, then restart: python3 scripts/pipeline_runner.py"
                )
                return False
            last_check = count
        last_count = count

    rc = proc.returncode
    if rc != 0:
        send_email(
            f"PIPELINE ERROR — {label} failed (exit {rc})",
            f"{label} exited with code {rc}.\n\nCheck log: {log_file}"
        )
        return False

    print(f"  DONE: {label}")
    return True


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print(f"\n{'='*60}")
    print("  LABI PIPELINE RUNNER")
    print(f"  Started: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"  Notify:  {NOTIFY_EMAIL}")
    print(f"  SMTP:    {'configured' if SMTP_PASS else 'NOT SET — emails skipped'}")
    print(f"{'='*60}\n")

    if not SMTP_PASS:
        print("  ⚠  No SMTP_PASS — add to .env to enable email alerts")
        print("     Get app password: https://myaccount.google.com/apppasswords\n")

    # Step 1: Normalise (skip if already done)
    raw_count  = len(list((SOURCES_DIR / "raw").glob("*.json")))
    norm_count = len(list((SOURCES_DIR / "normalised").glob("*.json")))

    if norm_count < raw_count * 0.95:
        print(f"  Normalise: {norm_count}/{raw_count} done — running...")
        ok = run_step(
            [sys.executable, "-u", "scripts/normalise.py", "--source", "protocols_io"],
            "/tmp/normalise.log",
            "Normalise"
        )
        if not ok:
            print("  Normalise failed — stopping pipeline")
            return
    else:
        print(f"  Normalise: already done ({norm_count} files) — skipping")

    # Final quality check after normalise
    stats = run_quality_check()
    print(f"\n  Post-normalise quality: {json.dumps(stats)}")
    if quality_is_bad(stats):
        send_email("PIPELINE ALERT — Quality issues after normalise", json.dumps(stats, indent=2))

    # Step 2: Fill step titles
    print("\n  Running fill_step_titles...")
    ok = run_step(
        [sys.executable, "-u", "scripts/fill_step_titles.py", "--source", "protocols_io"],
        "/tmp/fill_titles.log",
        "Fill Step Titles"
    )
    if not ok:
        send_email("PIPELINE ERROR — fill_step_titles failed", "Check /tmp/fill_titles.log")
        return

    # Step 3: Merge + dedup
    print("\n  Running merge_and_dedup...")
    ok = run_step(
        [sys.executable, "-u", "scripts/merge_and_dedup.py"],
        "/tmp/merge.log",
        "Merge & Dedup"
    )
    if not ok:
        send_email("PIPELINE ERROR — merge_and_dedup failed", "Check /tmp/merge.log")
        return

    # Done
    cdn_count = len(list(pathlib.Path("protocols").glob("*.json"))) if pathlib.Path("protocols").exists() else 0
    final_stats = run_quality_check()
    send_email(
        "Pipeline COMPLETE",
        f"All steps finished successfully.\n\n"
        f"Normalised: {norm_count} protocols\n"
        f"CDN-ready:  {cdn_count} protocols\n\n"
        f"Quality: {json.dumps(final_stats, indent=2)}\n\n"
        f"Next step: push to GitHub and update Flutter app."
    )
    print(f"\n  PIPELINE COMPLETE — {cdn_count} CDN-ready protocols")


if __name__ == "__main__":
    main()
