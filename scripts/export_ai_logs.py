#!/usr/bin/env python3
"""Copy raw Claude Code session logs into ai-logs/, redacting credentials.

Re-run before every submission-relevant commit: the live session file keeps
growing, and each run overwrites the exported copy with the latest version.

    python3 scripts/export_ai_logs.py [--source DIR] [--redact-email ADDR ...]

Redaction is deliberately narrow: credentials and the listed personal email
addresses only. The working process (prompts, tool calls, outputs) is kept
intact, as the assignment requires. Exits 1 if a credential-like string
survives redaction or if a redacted line stops being valid JSON.
"""
import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SOURCE = (Path.home() / ".claude" / "projects"
                  / "c--Users-aman--Downloads-data-engineer-assessment")
DEST = ROOT / "ai-logs" / "claude-code"

SECRET_PATTERNS = {
    "anthropic_key": r"sk-ant-[A-Za-z0-9_-]{10,}",
    "openai_key": r"sk-(?:proj-)?[A-Za-z0-9_-]{20,}",
    "github_token": r"gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}",
    "aws_key": r"AKIA[0-9A-Z]{16}",
    "bearer": r"[Bb]earer\s+[A-Za-z0-9._~+/-]{20,}",
    "jwt": r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}",
    "private_key": r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
}


def redact(text: str, emails) -> tuple[str, dict]:
    counts = {}
    for name, pattern in SECRET_PATTERNS.items():
        text, n = re.subn(pattern, f"<redacted-{name}>", text)
        if n:
            counts[name] = n
    for addr in emails:
        text, n = re.subn(re.escape(addr), "<redacted-email>", text, flags=re.IGNORECASE)
        if n:
            counts[f"email:{addr}"] = n
    return text, counts


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    ap.add_argument("--redact-email", action="append", default=[],
                    help="extra address to redact (git user.email is always redacted)")
    args = ap.parse_args()
    git_email = subprocess.run(["git", "config", "user.email"], cwd=ROOT,
                               capture_output=True, text=True).stdout.strip()
    if git_email:
        args.redact_email.append(git_email)

    sessions = sorted(args.source.glob("*.jsonl"))
    if not sessions:
        print(f"no session logs found in {args.source}", file=sys.stderr)
        return 1
    DEST.mkdir(parents=True, exist_ok=True)
    ok = True
    for src in sessions:
        text, counts = redact(src.read_text(encoding="utf-8"), args.redact_email)
        for i, line in enumerate(text.splitlines(), 1):
            if line.strip():
                try:
                    json.loads(line)
                except ValueError:
                    print(f"{src.name}:{i}: invalid JSON after redaction", file=sys.stderr)
                    ok = False
        leftovers = [n for n, p in SECRET_PATTERNS.items() if re.search(p, text)]
        if leftovers:
            print(f"{src.name}: credential patterns remain: {leftovers}", file=sys.stderr)
            ok = False
        (DEST / src.name).write_text(text, encoding="utf-8", newline="\n")
        print(f"{src.name}: {len(text.splitlines())} lines, redactions {counts or 'none'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
