#!/usr/bin/env python3
"""PostToolUse: format edited .py files with ruff if it is installed. Never fails the run."""
import json
import shutil
import subprocess
import sys

try:
    payload = json.load(sys.stdin)
    path = (payload.get("tool_input") or {}).get("file_path", "")
except Exception:
    sys.exit(0)

if path.endswith(".py") and shutil.which("ruff"):
    subprocess.run(["ruff", "format", "-q", path], check=False)
    subprocess.run(["ruff", "check", "--fix", "-q", path], check=False)

sys.exit(0)
