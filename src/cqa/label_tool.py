"""Manual labelling helper (stdlib only, Python 3.9+).

Usage:
  python3 src/cqa/label_tool.py show <conversation_id>
      print the dialog with message_index (0-based), speaker and text,
      then the checklist criteria for reference
  python3 src/cqa/label_tool.py template <id1> <id2> ... [--force]
      create docs/labels_template.csv: one row per dialog x criterion
      (columns: conversation_id, criterion, answer, message_index, reason)

Reads data/raw/dev.json and src/cqa/checklist.default.json.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CHECKLIST = Path(__file__).resolve().parent / "checklist.default.json"
DEV = ROOT / "data" / "raw" / "dev.json"
TEMPLATE = ROOT / "docs" / "labels_template.csv"
COLUMNS = ["conversation_id", "criterion", "answer", "message_index", "reason"]


def load_checklist() -> dict:
    return json.loads(CHECKLIST.read_text(encoding="utf-8"))


def criteria_sorted(checklist: dict) -> list:
    return sorted(checklist["criteria"], key=lambda c: c.get("order", 0))


def load_records() -> dict:
    if not DEV.exists():
        sys.exit(f"{DEV} not found: run python3 src/cqa/fetch_data.py first")
    records = json.loads(DEV.read_text(encoding="utf-8"))["data"]["records"]
    return {str(r["conversation_id"]): r for r in records}


def allowed_answers(c: dict) -> list:
    if c["type"] == "scale":
        vals = [str(s) for s in c.get("steps", [])]
    else:  # yes_no, flag
        vals = ["yes", "no"]
    if c.get("allow_na"):
        vals.append("na")
    return vals


def cmd_show(conv_id: str) -> None:
    rec = load_records().get(conv_id)
    if rec is None:
        sys.exit(f"unknown conversation_id: {conv_id}")
    transcript = rec.get("transcript") or []
    print(f"{conv_id}  language={rec.get('language')}  channel={rec.get('channel')}  "
          f"messages={len(transcript)}")
    print("=" * 80)
    for i, m in enumerate(transcript):
        print(f"[{i:>3}] {m.get('timestamp', '')} {str(m.get('speaker', '')).upper()}:")
        for line in str(m.get("text", "")).splitlines() or [""]:
            print(textwrap.fill(line, width=100, initial_indent="      ", subsequent_indent="      "))
    print("=" * 80)
    print("Criteria (answer column: one of the allowed values):")
    for c in criteria_sorted(load_checklist()):
        print(f"\n- {c['id']}: {c.get('title', '')}  [type={c['type']}, weight={c.get('weight')}]")
        print(f"  allowed: {', '.join(allowed_answers(c))}")
        if c.get("na_condition"):
            print(f"  na_condition: {c['na_condition']}")


def cmd_template(conv_ids: list, force: bool) -> None:
    records = load_records()
    unknown = [cid for cid in conv_ids if cid not in records]
    if unknown:
        sys.exit(f"unknown conversation_id(s): {', '.join(unknown)}")
    if TEMPLATE.exists() and not force:
        sys.exit(f"{TEMPLATE} already exists (may contain labels). Use --force to overwrite.")
    conv_ids = list(dict.fromkeys(conv_ids))  # dedupe, keep order
    crit_ids = [c["id"] for c in criteria_sorted(load_checklist())]
    TEMPLATE.parent.mkdir(parents=True, exist_ok=True)
    with TEMPLATE.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(COLUMNS)
        for cid in conv_ids:
            for crit in crit_ids:
                w.writerow([cid, crit, "", "", ""])
    print(f"wrote {TEMPLATE.relative_to(ROOT)}: {len(conv_ids)} dialogs x {len(crit_ids)} criteria "
          f"= {len(conv_ids) * len(crit_ids)} rows")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")
    sub.required = True
    p_show = sub.add_parser("show", help="print dialog with message_index and the checklist")
    p_show.add_argument("conversation_id")
    p_tpl = sub.add_parser("template", help="create docs/labels_template.csv")
    p_tpl.add_argument("conversation_ids", nargs="+")
    p_tpl.add_argument("--force", action="store_true", help="overwrite existing template")
    args = ap.parse_args()
    if args.cmd == "show":
        cmd_show(args.conversation_id)
    else:
        cmd_template(args.conversation_ids, args.force)


if __name__ == "__main__":
    main()
