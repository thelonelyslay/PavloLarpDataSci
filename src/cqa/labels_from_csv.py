"""Build data/labels.json from manual label files docs/labels_*.csv (except labels_template.csv).

CSV columns: conversation_id,criterion,answer,message_index,reason
- criterion: id (contact, needs, ...) in any case, or number 1..N in the checklist order;
- answer: yes/no/na or a scale step, any case; n/a, N/A, NA mean na; да/нет also work;
- extra columns (unquoted commas in reason) are glued back into reason;
- rows with an empty answer are skipped;
- answers are checked against the checklist of the dialog's channel
  (checklist.default.json for chats, checklist.calls.json for calls);
- if files disagree on (dialog, criterion), the pair is NOT written to labels.json:
  it goes to data/labels_conflicts.json and is printed.

Usage: python3 src/cqa/labels_from_csv.py
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any, Optional

from prompting import answer_options, criteria_sorted

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
FIELDS = ["conversation_id", "criterion", "answer", "message_index", "reason"]
ALIASES = {"да": "yes", "нет": "no", "n/a": "na", "n.a.": "na", "н/п": "na", "н/а": "na",
           "true": "yes", "false": "no"}


def resolve_criterion(checklist: dict, raw: str) -> tuple[Optional[dict], str]:
    """Criterion by id (case-insensitive) or by number 1..N in checklist order; (None, why) if not found."""
    key = raw.strip()
    ordered = criteria_sorted(checklist)
    if key.isdigit():
        n = int(key)
        if 1 <= n <= len(ordered):
            return ordered[n - 1], ""
        return None, f"criterion number {n} out of range 1..{len(ordered)}"
    found = next((c for c in ordered if c["id"].lower() == key.lower()), None)
    return found, "" if found else f"unknown criterion {raw!r}"


def load_checklists() -> tuple[dict, dict]:
    """Return ({applies_to: checklist}, default checklist)."""
    by_channel = {}
    default = None
    for name in ("checklist.default.json", "checklist.calls.json"):
        ck = json.loads((HERE / name).read_text(encoding="utf-8"))
        by_channel.setdefault(ck.get("applies_to"), ck)
        if name == "checklist.default.json":
            default = ck
    return by_channel, default


def load_channels(dev: Path) -> dict[str, str]:
    if not dev.exists():
        return {}
    records = json.loads(dev.read_text(encoding="utf-8"))["data"]["records"]
    return {r["conversation_id"]: r.get("channel") for r in records}


def normalize(criterion: dict, raw: str) -> Optional[Any]:
    """Allowed answer in labels.json form ("yes"/"no"/"na" or a number), or None if invalid."""
    v = raw.strip().lower()
    v = ALIASES.get(v, v)
    if criterion["type"] == "scale" and v != "na":
        try:
            f = float(v.replace(",", "."))
            v = str(int(f)) if f.is_integer() else str(f)
        except ValueError:
            return None
    if v not in answer_options(criterion):
        return None
    if criterion["type"] == "scale" and v != "na":
        f = float(v)
        return int(f) if f.is_integer() else f
    return v


def read_rows(path: Path):
    """Yield (line, row-as-dict). Header and blank lines are skipped."""
    with path.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.reader(fh)
        for row in reader:
            line = reader.line_num
            if not row or not any(cell.strip() for cell in row):
                continue
            if row[0].strip().lower() == FIELDS[0]:
                continue
            if len(row) > len(FIELDS):
                row = row[:4] + [",".join(row[4:])]
            row = row + [""] * (len(FIELDS) - len(row))
            yield line, {k: v.strip() for k, v in zip(FIELDS, row)}


def build(docs: Path, dev: Path) -> tuple[dict, dict, list[str], int]:
    """Return (labels, conflicts, errors, files_read)."""
    by_channel, default = load_checklists()
    channels = load_channels(dev)
    files = sorted(p for p in docs.glob("labels_*.csv") if p.name != "labels_template.csv")
    votes: dict[tuple[str, str], list[dict]] = {}
    errors: list[str] = []
    for path in files:
        rel = path.relative_to(ROOT) if ROOT in path.parents else path
        for line, r in read_rows(path):
            if not r["answer"]:
                continue
            where = f"{rel}:{line}"
            conv, crit = r["conversation_id"], r["criterion"]
            if not conv or not crit:
                errors.append(f"{where}: empty conversation_id or criterion")
                continue
            checklist = by_channel.get(channels.get(conv), default)
            criterion, why = resolve_criterion(checklist, crit)
            if criterion is None:
                errors.append(f"{where}: {why}")
                continue
            crit = criterion["id"]
            value = normalize(criterion, r["answer"])
            if value is None:
                errors.append(f"{where}: {conv}/{crit}: answer {r['answer']!r} not in {answer_options(criterion)}")
                continue
            votes.setdefault((conv, crit), []).append(
                {"file": str(rel), "line": line, "answer": value,
                 "message_index": r["message_index"], "reason": r["reason"]})

    labels: dict[str, dict] = {}
    conflicts: dict[str, dict] = {}
    for (conv, crit), vs in sorted(votes.items()):
        if len({json.dumps(v["answer"]) for v in vs}) > 1:
            conflicts.setdefault(conv, {})[crit] = vs
        else:
            labels.setdefault(conv, {})[crit] = vs[0]["answer"]
    return labels, conflicts, errors, len(files)


def main(argv: Optional[list[str]] = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--docs", default=str(ROOT / "docs"))
    ap.add_argument("--dev", default=str(ROOT / "data" / "raw" / "dev.json"))
    ap.add_argument("--out", default=str(ROOT / "data" / "labels.json"))
    ap.add_argument("--conflicts", default=str(ROOT / "data" / "labels_conflicts.json"))
    args = ap.parse_args(argv)

    labels, conflicts, errors, n_files = build(Path(args.docs), Path(args.dev))
    for out, data in ((Path(args.out), labels), (Path(args.conflicts), conflicts)):
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")

    if errors:
        print("errors (not written):")
        for e in errors:
            print("  ", e)
    if conflicts:
        print("conflicts (not written to labels.json):")
        for conv, crits in conflicts.items():
            for crit, vs in crits.items():
                print(f"   {conv}/{crit}: " + "; ".join(f"{v['file']}:{v['line']}={v['answer']}" for v in vs))
    n_conf = sum(len(v) for v in conflicts.values())
    n_labels = sum(len(v) for v in labels.values())
    print(f"files: {n_files}, dialogs: {len(labels)}, labels: {n_labels}, "
          f"conflicts: {n_conf}, errors: {len(errors)}")
    print(f"-> {args.out}" + (f", {args.conflicts}" if n_conf else ""))


if __name__ == "__main__":
    main()
