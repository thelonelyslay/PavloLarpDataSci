"""Evaluate one conversation by a checklist: prompting -> llm -> parse_answer -> scoring.score.

The model only gives answers and evidence; points and totals come from scoring.py.
Nothing is cached or precomputed: every call sends the dialog to the model.

Python:
    from evaluator import evaluate
    result = evaluate(record, checklist)                  # model chain from EVAL_MODELS in .env
    result = evaluate(record, checklist, "claude-opus-5-5")  # one model, no fallback

CLI:
    python3 src/cqa/evaluator.py --id p1-ru-001 [--model claude-sonnet-5-5] [--checklist path.json]

Result format: AGENTS.md -> "Результат оценки (JSON)".
"""
from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path
from typing import Any, Optional

import llm
import scoring
from prompting import ParseError, build_schema, build_system_prompt, build_user_prompt, criteria_sorted, parse_answer

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
DEV = ROOT / "data" / "raw" / "dev.json"
CHECKLIST_FILES = [HERE / "checklist.default.json", HERE / "checklist.calls.json"]  # first = default
DEFAULT_OPTS = {"effort": "low", "gemini_thinking": "auto"}
ATTEMPTS_PER_MODEL = 2  # 1 retry on API error (after HTTP retries) or invalid answer, then next model


class EvaluationError(RuntimeError):
    """All models in the chain failed."""


def model_chain(env: dict, model_name: Optional[str] = None) -> list[str]:
    """Explicit model -> just it. Otherwise EVAL_MODELS (comma-separated), else all configured models."""
    if model_name:
        return [model_name]
    names = [m.strip() for m in (env.get("EVAL_MODELS") or "").split(",") if m.strip()]
    return names or [s.name for s in llm.available_models(env)]


def load_checklist(path: Path) -> dict:
    checklist = json.loads(Path(path).read_text(encoding="utf-8"))
    checklist.setdefault("id", Path(path).stem)
    scoring.validate_checklist(checklist)
    return checklist


def checklist_for_channel(channel: Optional[str]) -> dict:
    """Checklist whose applies_to matches the channel; checklist.default.json otherwise."""
    loaded = [load_checklist(p) for p in CHECKLIST_FILES]
    return next((c for c in loaded if c.get("applies_to") == channel), loaded[0])


def _answer_out(v: Any) -> Any:
    if isinstance(v, float) and v.is_integer():
        return int(v)
    return v


def build_result(record: dict, checklist: dict, answers: dict, details: dict,
                 model: str, cost_usd: Optional[float]) -> dict:
    """Assemble the AGENTS.md result. All numbers come from scoring.score()."""
    scored = scoring.score(checklist, answers)
    by_id = {c["id"]: c for c in scored["criteria"]}  # flags are not in scored["criteria"]
    last = max(len(record.get("transcript") or []) - 1, 0)
    criteria = []
    for c in criteria_sorted(checklist):
        cid = c["id"]
        s = by_id.get(cid)
        answer = _answer_out(s["answer"] if s else answers[cid])
        explanation = details[cid]["explanation"]
        criteria.append({
            "id": cid,
            "title": c["title"],
            "answer": answer,
            "original_answer": answer,
            "final_answer": answer,
            "applicable": s["applicable"] if s else True,
            "weight": c["weight"],
            "effective_weight": s["effective_weight"] if s else 0.0,
            "points": s["points"] if s else 0.0,
            "explanation": explanation,
            "na_reason": explanation if answer == "na" else None,
            "evidence": details[cid]["evidence"],
            "checked_range": [0, last],
            "appeal": None,
        })
    return {
        "id": uuid.uuid4().hex,
        "conversation_id": record.get("conversation_id"),
        "checklist_id": checklist.get("id"),
        "checklist_version": checklist.get("version"),
        "model": model,
        "cost_usd": round(cost_usd, 6) if cost_usd is not None else None,
        "status": "done",
        "total": scored["total"],
        "original_total": scored["total"],
        "section_scores": scored["section_scores"],
        "gross_error": scored["gross_error"],
        "criteria": criteria,
    }


def evaluate(record: dict, checklist: dict, model_name: Optional[str] = None, *,
             opts: Optional[dict] = None, env: Optional[dict] = None,
             stats: Optional[dict] = None) -> dict:
    """Return the evaluation JSON from AGENTS.md; field `model` is the model that actually answered.

    opts:  {"effort": Claude effort, "gemini_thinking": ...} (defaults in DEFAULT_OPTS)
    env:   settings/keys (default: .env)
    stats: optional dict filled with seconds, tokens, attempts, errors, warnings, params (for comparisons)
    Raises EvaluationError if every model in the chain failed.
    """
    env = llm.load_env() if env is None else env
    opts = {**DEFAULT_OPTS, **(opts or {})}
    stats = {} if stats is None else stats
    stats.update(model=None, seconds=0.0, input_tokens=0, output_tokens=0, cost_usd=None,
                 attempts=0, errors=[], warnings=[], params=None)
    chain = model_chain(env, model_name)
    if not chain:
        raise EvaluationError("no models configured: set EVAL_MODELS and API keys in .env")

    system, schema, user = build_system_prompt(checklist), build_schema(checklist), build_user_prompt(record)
    for name in chain:
        spec = llm.resolve_model(name, env)
        if spec is None:
            stats["errors"].append({"model": name, "error": "no API key or unknown model"})
            continue
        for _ in range(ATTEMPTS_PER_MODEL):
            stats["attempts"] += 1
            try:
                res = llm.call(spec, env, system, user, schema, opts)
            except llm.LLMError as e:
                stats["errors"].append({"model": name, "error": f"api: {e}"})
                continue
            stats["seconds"] += res.seconds
            stats["input_tokens"] += res.input_tokens
            stats["output_tokens"] += res.output_tokens
            if res.cost_usd is not None:
                stats["cost_usd"] = (stats["cost_usd"] or 0.0) + res.cost_usd
            stats["params"] = res.params
            try:
                answers, details, warnings = parse_answer(res.text, checklist, record)
                result = build_result(record, checklist, answers, details, spec.name, stats["cost_usd"])
            except (ParseError, ValueError) as e:
                stats["errors"].append({"model": name, "error": f"parse: {e}", "raw_output": res.text[:2000]})
                continue
            stats.update(model=spec.name, warnings=warnings)
            return result

    errs = stats["errors"]
    if len(chain) == 1:
        msg = errs[-1]["error"] if errs else "evaluation failed"
    else:
        msg = "; ".join(f"{e['model']}: {e['error']}" for e in errs)
    raise EvaluationError(msg)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--id", required=True, help="conversation_id from data/raw/dev.json")
    ap.add_argument("--model", help="one model, no fallback (default: EVAL_MODELS chain from .env)")
    ap.add_argument("--checklist", help="checklist JSON (default: by channel, chats -> checklist.default.json)")
    ap.add_argument("--dev", default=str(DEV), help="conversations file")
    args = ap.parse_args()

    dev = Path(args.dev)
    if not dev.exists():
        sys.exit(f"{dev} not found: run python3 src/cqa/fetch_data.py first")
    records = json.loads(dev.read_text(encoding="utf-8"))["data"]["records"]
    record = next((r for r in records if r.get("conversation_id") == args.id), None)
    if record is None:
        sys.exit(f"conversation {args.id!r} not found in {dev}")
    checklist = load_checklist(Path(args.checklist)) if args.checklist else checklist_for_channel(record.get("channel"))
    try:
        result = evaluate(record, checklist, args.model)
    except EvaluationError as e:
        sys.exit(f"evaluation failed: {e}")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
