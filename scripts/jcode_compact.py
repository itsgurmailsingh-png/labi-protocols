#!/usr/bin/env python3
"""
jcode_compact.py
Context compaction wrapper for jcode.

Usage:
  python3 jcode_compact.py                    # start interactive session
  python3 jcode_compact.py --session mysession # resume named session

How it works:
  - Tracks token usage across your jcode session
  - When approaching context limit (~80%), summarizes the conversation
  - Injects the summary as a system message and continues cleanly
  - Saves session history to ~/.jcode_sessions/

Requirements:
  pip install openai tiktoken
  Set OPENAI_API_KEY or ANTHROPIC_API_KEY in your env (or .env file)
"""

import os, sys, json, time, subprocess, signal
from pathlib import Path
from datetime import datetime

try:
    import tiktoken
    from openai import OpenAI
except ImportError:
    print("[ERROR] Missing deps. Run: pip install openai tiktoken")
    sys.exit(1)

# ── Config ──────────────────────────────────────────────────────────────────
SESSIONS_DIR    = Path.home() / ".jcode_sessions"
CONTEXT_LIMIT   = 128_000   # tokens — adjust for your model
COMPACT_AT      = 0.80      # compact when at 80% of limit
SUMMARY_MODEL   = "gpt-4o-mini"  # cheap summarizer
SUMMARY_MAX_TOK = 800       # how long the summary should be

# ── Load env ─────────────────────────────────────────────────────────────────
env_file = Path(__file__).parent.parent / ".env"
if env_file.exists():
    for line in env_file.read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

OPENAI_KEY = os.environ.get("OPENAI_API_KEY", "")
if not OPENAI_KEY:
    print("[ERROR] OPENAI_API_KEY not set (needed for summarization)")
    sys.exit(1)

client  = OpenAI(api_key=OPENAI_KEY)
enc     = tiktoken.get_encoding("cl100k_base")

SESSIONS_DIR.mkdir(exist_ok=True)


def count_tokens(text: str) -> int:
    return len(enc.encode(text))


def summarize(messages: list[dict]) -> str:
    """Summarize a conversation history into a compact context block."""
    convo_text = "\n\n".join(
        f"[{m['role'].upper()}]: {m['content']}"
        for m in messages
        if m.get("content")
    )
    prompt = f"""You are summarizing a coding assistant conversation for context compaction.
Create a dense, structured summary covering:
1. What was being built / the goal
2. Key decisions made and why
3. Files created or modified (with paths)
4. Current state — what works, what's pending
5. Any blockers or errors encountered

Be terse. Preserve file paths, variable names, and technical specifics exactly.
Conversation to summarize:

{convo_text[:12000]}"""

    resp = client.chat.completions.create(
        model=SUMMARY_MODEL,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=SUMMARY_MAX_TOK,
        temperature=0.1,
    )
    return resp.choices[0].message.content.strip()


class CompactionSession:
    def __init__(self, name: str = None):
        ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
        name = name or ts
        self.session_file = SESSIONS_DIR / f"{name}.json"
        self.messages: list[dict] = []
        self.total_tokens = 0
        self.compact_count = 0

        if self.session_file.exists():
            data = json.loads(self.session_file.read_text())
            self.messages    = data.get("messages", [])
            self.compact_count = data.get("compact_count", 0)
            print(f"[compact] Resumed session '{name}' — {len(self.messages)} messages, compacted {self.compact_count}x")
        else:
            print(f"[compact] New session '{name}'")

    def save(self):
        self.session_file.write_text(json.dumps({
            "messages":      self.messages,
            "compact_count": self.compact_count,
            "saved_at":      datetime.now().isoformat(),
        }, indent=2))

    def add(self, role: str, content: str):
        self.messages.append({"role": role, "content": content})
        self.total_tokens = sum(count_tokens(m.get("content","")) for m in self.messages)
        self.save()

        used_pct = self.total_tokens / CONTEXT_LIMIT
        print(f"  [tokens] {self.total_tokens:,} / {CONTEXT_LIMIT:,} ({used_pct:.0%})", end="")

        if used_pct >= COMPACT_AT:
            print(" → COMPACTING...")
            self._compact()
        else:
            print()

    def _compact(self):
        """Summarize old messages and replace with a single context block."""
        print("[compact] Summarizing conversation history...")
        summary = summarize(self.messages)
        self.compact_count += 1

        # Keep only last 3 messages (most recent context), prepend summary
        recent = self.messages[-3:] if len(self.messages) > 3 else []
        self.messages = [
            {
                "role": "system",
                "content": (
                    f"[CONTEXT SUMMARY — compaction #{self.compact_count}]\n\n"
                    f"{summary}\n\n"
                    "[End of summary. Continue from the current state above.]"
                )
            },
            *recent
        ]
        self.total_tokens = sum(count_tokens(m.get("content","")) for m in self.messages)
        self.save()
        print(f"[compact] Done. Context reduced to {self.total_tokens:,} tokens.")
        print(f"[compact] Summary preview: {summary[:200]}...")

    def context_for_jcode(self) -> str:
        """Return the session context to inject into jcode as initial prompt."""
        if not self.messages:
            return ""
        parts = []
        for m in self.messages:
            role = m["role"].upper()
            content = m.get("content", "")
            parts.append(f"[{role}]: {content}")
        return "\n\n".join(parts)


def run_jcode_with_compaction(session: CompactionSession):
    """Interactive loop: read user input → call jcode → track tokens → compact if needed."""
    print("\n[jcode+compact] Type your message. 'quit' to exit, 'status' to see token usage.\n")

    while True:
        try:
            user_input = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n[compact] Session saved.")
            break

        if not user_input:
            continue
        if user_input.lower() in ("quit", "exit", "q"):
            print("[compact] Session saved.")
            break
        if user_input.lower() == "status":
            pct = session.total_tokens / CONTEXT_LIMIT
            print(f"  tokens: {session.total_tokens:,} / {CONTEXT_LIMIT:,} ({pct:.0%})")
            print(f"  compactions: {session.compact_count}")
            print(f"  messages: {len(session.messages)}")
            continue

        # Build context injection — pass prior session as initial context
        context = session.context_for_jcode()
        full_prompt = f"{context}\n\n[USER]: {user_input}" if context else user_input

        # Write prompt to temp file to avoid shell escaping issues
        tmp = Path("/tmp/jcode_prompt.txt")
        tmp.write_text(full_prompt)

        print("jcode> ", end="", flush=True)
        try:
            # Run jcode with the prompt piped in
            result = subprocess.run(
                ["jcode", "--print", "--no-interactive"],
                input=full_prompt,
                capture_output=True,
                text=True,
                timeout=300,
            )
            response = result.stdout.strip() or result.stderr.strip()
        except subprocess.TimeoutExpired:
            response = "[ERROR] jcode timed out after 5 minutes"
        except FileNotFoundError:
            # Try full path
            jcode_path = Path.home() / ".local/bin/jcode"
            if jcode_path.exists():
                result = subprocess.run(
                    [str(jcode_path), "--print", "--no-interactive"],
                    input=full_prompt,
                    capture_output=True,
                    text=True,
                    timeout=300,
                )
                response = result.stdout.strip() or result.stderr.strip()
            else:
                response = "[ERROR] jcode not found"

        print(response)

        # Track both sides
        session.add("user", user_input)
        session.add("assistant", response)


def main():
    import argparse
    parser = argparse.ArgumentParser(description="jcode with auto-compaction")
    parser.add_argument("--session", "-s", default=None, help="Session name to create or resume")
    parser.add_argument("--list",    "-l", action="store_true", help="List saved sessions")
    args = parser.parse_args()

    if args.list:
        sessions = sorted(SESSIONS_DIR.glob("*.json"))
        if not sessions:
            print("No saved sessions.")
        for f in sessions:
            data = json.loads(f.read_text())
            msgs = len(data.get("messages", []))
            saved = data.get("saved_at", "?")
            print(f"  {f.stem:30s}  {msgs:3d} messages  saved {saved[:16]}")
        return

    session = CompactionSession(name=args.session)
    run_jcode_with_compaction(session)


if __name__ == "__main__":
    main()
