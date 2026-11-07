"""Compare LLMs on a few DEV dialogs: accuracy (if data/labels.json exists), latency, tokens, cost.

Usage:
  python3 src/cqa/compare_models.py                    # all models with keys in .env
  python3 src/cqa/compare_models.py --models claude-haiku-5-5 --n-ru 1 --n-kz 0 --n-en 0
  python3 src/cqa/compare_models.py --dry-run          # build prompts only, no API calls

Total score is computed only by scoring.py. Results: data/results/<model>.json
labels.json format: {"<conversation_id>": {"<criterion_id>": "yes"|"no"|"na"|<step>, ...}, ...}
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import evaluator
import llm
import scoring
from prompting import build_schema, build_system_prompt, build_user_prompt

ROOT = Path(__file__).resolve().parents[2]
CHECKLIST = Path(__file__).resolve().parent / "checklist.default.json"
DEV = ROOT / "data" / "raw" / "dev.json"
LABELS = ROOT / "data" / "labels.json"
RESULTS = ROOT / "data" / "results"


def load_labels(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    # accept {"id": {"answers": {...}}} as well as {"id": {...}}
    return {cid: (v.get("answers", v) if isinstance(v, dict) else {}) for cid, v in raw.items()}


def pick_dialogs(records: list[dict], labels: dict, channel: str, quota: dict[str, int]) -> list[dict]:
    """Deterministic pick: labelled dialogs first, then by conversation_id."""
    pool = [r for r in records if not channel or r.get("channel") == channel]
    pool.sort(key=lambda r: (r["conversation_id"] not in labels, str(r["conversation_id"])))
    chosen = []
    for lang, n in quota.items():
        got = [r for r in pool if r.get("language") == lang][:n]
        if len(got) < n:
            print(f"warning: only {len(got)} {lang} dialogs for channel={channel!r} (asked {n})")
        chosen += got
    return chosen


def norm_answer(v: Any) -> Any:
    if isinstance(v, str):
        s = v.strip().lower()
        s = {"да": "yes", "нет": "no", "n/a": "na", "н/п": "na", "true": "yes", "false": "no"}.get(s, s)
        try:
            return float(s)
        except ValueError:
            return s
    if isinstance(v, bool):
        return "yes" if v else "no"
    return float(v) if isinstance(v, (int, float)) else v


def evaluate_one(spec: llm.ModelSpec, env: dict, checklist: dict, record: dict, opts: dict) -> dict:
    """One dialog with one model (no fallback) via evaluator.evaluate + timing/tokens for the table."""
    stats: dict = {}
    out: dict[str, Any] = {"conversation_id": record["conversation_id"], "language": record.get("language"),
                           "channel": record.get("channel"), "error": None}
    try:
        res = evaluator.evaluate(record, checklist, spec.name, opts=opts, env=env, stats=stats)
    except evaluator.EvaluationError as e:
        res = None
        out["error"] = str(e)
        if stats.get("errors") and "raw_output" in stats["errors"][-1]:
            out["raw_output"] = stats["errors"][-1]["raw_output"]
    for k in ("seconds", "input_tokens", "output_tokens", "cost_usd", "attempts"):
        out[k] = stats.get(k)
    out["params"] = stats.get("params")
    if res is not None:
        out.update(evaluation_id=res["id"], answers={c["id"]: c["answer"] for c in res["criteria"]},
                   total=res["total"], section_scores=res["section_scores"], gross_error=res["gross_error"],
                   criteria=res["criteria"], warnings=stats.get("warnings", []))
    return out


def run_model(spec: llm.ModelSpec, env: dict, checklist: dict, dialogs: list[dict], opts: dict) -> dict:
    rows = []
    for rec in dialogs:
        r = evaluate_one(spec, env, checklist, rec, opts)
        status = "ok" if not r["error"] else r["error"][:80]
        print(f"  {spec.name:<22} {rec['conversation_id']:<14} {r['seconds']:6.1f}s  {status}", flush=True)
        rows.append(r)
    result = {
        "model": spec.name, "provider": spec.provider, "model_id": spec.model_id,
        "price_per_1m": {"input": spec.price_in, "output": spec.price_out},
        "checklist": checklist.get("name"), "checklist_version": checklist.get("version"),
        "effort": opts.get("effort") if spec.provider == "anthropic" else None,
        "gemini_thinking": opts.get("gemini_thinking") if spec.provider == "gemini" else None,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "dialogs": rows,
    }
    RESULTS.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", spec.name)
    (RESULTS / f"{safe}.json").write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    return result


def accuracy(result: dict, labels: dict, checklist: dict) -> Optional[dict]:
    """Share of matching answers on labelled criteria + mean |total diff| where labels are complete."""
    hit = tot = 0
    per_crit: dict[str, list[int]] = {}
    diffs = []
    ids = [c["id"] for c in checklist["criteria"]]
    for d in result["dialogs"]:
        gt = labels.get(d["conversation_id"])
        if not gt or d.get("error"):
            continue
        for cid in ids:
            if cid not in gt:
                continue
            ok = int(norm_answer(gt[cid]) == norm_answer(d["answers"][cid]))
            hit += ok
            tot += 1
            per_crit.setdefault(cid, [0, 0])
            per_crit[cid][0] += ok
            per_crit[cid][1] += 1
        if all(cid in gt for cid in ids):
            try:
                gt_total = scoring.score(checklist, gt)["total"]
            except ValueError as e:
                print(f"warning: bad label for {d['conversation_id']}: {e}")
                continue
            if gt_total is not None and d.get("total") is not None:
                diffs.append(abs(gt_total - d["total"]))
    if not tot:
        return None
    return {"acc": hit / tot, "n": tot, "per_crit": {k: v[0] / v[1] for k, v in per_crit.items()},
            "mae_total": (sum(diffs) / len(diffs)) if diffs else None}


def fmt_answer(v: Any) -> str:
    v = norm_answer(v)
    if isinstance(v, float):
        return str(int(v)) if v.is_integer() else str(v)
    return "—" if v is None else str(v)


def show_answers(results: list[dict], dialogs: list[dict], checklist: dict, labels: dict) -> None:
    """Per dialog: criterion x model answers (no dialog text, no quotes); mark disagreements."""
    ids = [c["id"] for c in sorted(checklist["criteria"], key=lambda c: c.get("order", 0))]
    names = [r["model"] for r in results]
    by_model = {r["model"]: {d["conversation_id"]: d for d in r["dialogs"]} for r in results}
    disagree_count: dict[str, int] = {}
    for rec in dialogs:
        conv = rec["conversation_id"]
        runs = [by_model[n].get(conv) for n in names]
        runs = [d if d and not d.get("error") else None for d in runs]
        gt = labels.get(conv)
        rows = [["criterion"] + names + (["label"] if gt else []) + ["diff"]]
        diverged = []
        for cid in ids:
            vals = [fmt_answer(d["answers"][cid]) if d else "ERR" for d in runs]
            differs = len({v for v in vals if v != "ERR"}) > 1
            if differs:
                diverged.append(cid)
                disagree_count[cid] = disagree_count.get(cid, 0) + 1
            rows.append([cid] + vals + ([fmt_answer(gt.get(cid))] if gt else []) + ["≠" if differs else ""])
        rows.append(["TOTAL"] + [f"{d['total']}" if d and d.get("total") is not None else "—" for d in runs]
                    + ([""] if gt else []) + [""])
        print(f"\n{conv} ({rec.get('language')}, {len(rec.get('transcript') or [])} messages)")
        print(fmt_table(rows))
        print("models disagree on:", ", ".join(diverged) if diverged else "none")
    if len(names) > 1:
        summary = ", ".join(f"{k} {v}/{len(dialogs)}" for k, v in
                            sorted(disagree_count.items(), key=lambda x: -x[1])) or "none"
        print(f"\ndisagreements by criterion (dialogs): {summary}")


def fmt_table(rows: list[list[str]]) -> str:
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    lines = [" | ".join(c.ljust(w) for c, w in zip(r, widths)) for r in rows]
    lines.insert(1, "-+-".join("-" * w for w in widths))
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--models", nargs="*", help="model names to run (default: all with keys)")
    ap.add_argument("--n-ru", type=int, default=2)
    ap.add_argument("--n-kz", type=int, default=2)
    ap.add_argument("--n-en", type=int, default=1)
    ap.add_argument("--channel", default="agent chat", help='"" = any channel')
    ap.add_argument("--effort", default=None, choices=["low", "medium", "high", "xhigh", "max"],
                    help="Claude effort level (default: EVAL_EFFORT in .env, else low; Claude takes no temperature)")
    ap.add_argument("--gemini-thinking", default="auto",
                    choices=["auto", "minimal", "low", "medium", "high", "default"],
                    help="Gemini thinkingLevel; auto = lowest level the model accepts, default = model's own")
    ap.add_argument("--show-answers", action="store_true",
                    help="print criterion x model answers per dialog and mark disagreements")
    ap.add_argument("--dry-run", action="store_true", help="build prompts and exit, no API calls")
    args = ap.parse_args()

    try:
        checklist = evaluator.load_checklist(CHECKLIST)
    except evaluator.ChecklistError as e:
        sys.exit(str(e))
    if not DEV.exists():
        sys.exit(f"{DEV} not found: run python3 src/cqa/fetch_data.py first")
    records = json.loads(DEV.read_text(encoding="utf-8"))["data"]["records"]
    labels = load_labels(LABELS)
    dialogs = pick_dialogs(records, labels, args.channel, {"RU": args.n_ru, "KZ": args.n_kz, "EN": args.n_en})
    if not dialogs:
        sys.exit("no dialogs selected")
    print(f"dialogs: {', '.join(d['conversation_id'] + '/' + str(d.get('language')) for d in dialogs)}")
    print(f"labels: {f'{LABELS.name}, {len(labels)} dialogs' if labels else 'none (no accuracy column)'}")

    if args.dry_run:
        system = build_system_prompt(checklist)
        users = [build_user_prompt(d) for d in dialogs]
        print(f"system prompt: {len(system)} chars; user prompts: {[len(u) for u in users]} chars")
        print(f"schema criteria: {list(build_schema(checklist)['properties'])}")
        return

    env = llm.load_env()
    specs = llm.available_models(env)
    if args.models:
        specs = [s for s in specs if s.name in args.models]
    if not specs:
        print("no models to run. Check .env:")
        for h in llm.missing_setup(env):
            print("  -", h)
        sys.exit(1)

    effort = args.effort or evaluator.effort_from_env(env)
    opts = {"effort": effort, "gemini_thinking": args.gemini_thinking}
    print(f"models: {', '.join(s.name for s in specs)}  (Claude: effort={effort}, no temperature; "
          f"Gemini: thinking={args.gemini_thinking}; OpenAI: temperature=0)")
    with ThreadPoolExecutor(max_workers=len(specs)) as pool:  # models in parallel, dialogs sequential
        results = list(pool.map(lambda s: run_model(s, env, checklist, dialogs, opts), specs))

    header = ["model", "ok", "sec/dialog", "tokens in/out per dialog", "$/dialog"]
    if labels:
        header += ["accuracy", "|Δtotal|"]
    rows = [header]
    accs = {}
    for res in results:
        ok = [d for d in res["dialogs"] if not d["error"]]
        n = max(len(ok), 1)
        costs = [d["cost_usd"] for d in ok if d["cost_usd"] is not None]
        row = [res["model"], f"{len(ok)}/{len(res['dialogs'])}",
               f"{sum(d['seconds'] for d in ok) / n:.1f}",
               f"{sum(d['input_tokens'] for d in ok) // n}/{sum(d['output_tokens'] for d in ok) // n}",
               f"{sum(costs) / len(costs):.4f}" if costs else "—"]
        if labels:
            a = accuracy(res, labels, checklist)
            accs[res["model"]] = a
            row += [f"{a['acc']:.0%} (n={a['n']})" if a else "—",
                    f"{a['mae_total']:.1f}" if a and a["mae_total"] is not None else "—"]
        rows.append(row)
    print()
    print(fmt_table(rows))

    if any(accs.values()):
        ids = [c["id"] for c in checklist["criteria"]]
        crow = [["model"] + ids]
        for name, a in accs.items():
            crow.append([name] + ([f"{a['per_crit'][i]:.0%}" if a and i in a["per_crit"] else "—" for i in ids]))
        print("\naccuracy by criterion:")
        print(fmt_table(crow))
    if args.show_answers:
        show_answers(results, dialogs, checklist, labels)
    print(f"\nresults: {RESULTS}/")


if __name__ == "__main__":
    main()
