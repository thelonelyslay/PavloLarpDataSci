#!/usr/bin/env python3
"""PreToolUse guard for Bash: blocks destructive commands and context-flooding dumps.
Exit code 2 blocks the call and feeds stderr back to Claude."""
import json
import re
import sys

try:
    payload = json.load(sys.stdin)
except Exception:
    sys.exit(0)

cmd = (payload.get("tool_input") or {}).get("command", "")

RULES = [
    (r"\brm\s+-[a-zA-Z]*r[a-zA-Z]*f|\brm\s+-[a-zA-Z]*f[a-zA-Z]*r", "rm -rf is blocked."),
    (r"git\s+push\s+.*--force|git\s+push\s+-f\b", "Force push is blocked."),
    (r"git\s+reset\s+--hard", "git reset --hard is blocked."),
    (r"\bcat\s+\S*data/\S*\.(csv|parquet|json|xlsx)", "Do not cat data files; use head or src/common.py:profile."),
    (r">\s*\S*data/raw/", "data/raw is read-only."),
]

for pattern, message in RULES:
    if re.search(pattern, cmd):
        print(f"BLOCKED: {message}", file=sys.stderr)
        sys.exit(2)

sys.exit(0)
