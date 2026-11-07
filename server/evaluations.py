"""Evaluation, batch processing, and appeal API."""
from __future__ import annotations

import hashlib
import importlib
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from src.cqa.scoring import score
router = APIRouter(prefix="/api")
ROOT = Path(__file__).resolve().parents[1]


def _connect():
    return importlib.import_module("server.app")._connect()


def get_conversation(conversation_id: str) -> dict:
    return importlib.import_module("server.app").get_conversation(conversation_id)


class EngineEvaluationError(Exception):
    pass


class EngineChecklistError(Exception):
    pass


def _run_engine(record: dict, checklist: dict) -> tuple[dict, dict]:
    """Invoke the merged evaluator with its expected record shape."""
    engine_path = str(ROOT / "src" / "cqa")
    if engine_path not in sys.path:
        sys.path.insert(0, engine_path)
    evaluator = importlib.import_module("evaluator")
    stats: dict = {}
    engine_record = {**record, "conversation_id": record["id"]}
    try:
        result = evaluator.evaluate(engine_record, checklist, stats=stats)
    except evaluator.ChecklistError as exc:
        raise EngineChecklistError(str(exc)) from None
    except evaluator.EvaluationError as exc:
        raise EngineEvaluationError(str(exc)) from None
    return result, stats


def _compose(evaluation_id: str, record: dict, checklist_id: str, checklist: dict,
             model: str, cost: float | None, status: str, original: dict,
             final: dict, details: dict, appeals: dict | None = None) -> dict:
    original_scored = score(checklist, original)
    scored = score(checklist, final)
    scored_by_id = {c["id"]: c for c in scored["criteria"]}
    criteria = []
    for c in checklist["criteria"]:
        cid = c["id"]
        item = scored_by_id.get(cid, {"id": cid, "title": c["title"], "applicable": True,
                                      "weight": c["weight"], "effective_weight": 0, "points": 0})
        detail = details.get(cid) or {}
        answer = final[cid]
        criteria.append({**item, "answer": answer, "original_answer": original[cid],
                         "final_answer": answer, "explanation": detail.get("explanation", ""),
                         "na_reason": detail.get("explanation", "") if answer == "na" else None,
                         "evidence": detail.get("evidence", []),
                         "checked_range": [0, max(0, len(record["transcript"]) - 1)],
                         "appeal": (appeals or {}).get(cid)})
    return {"id": evaluation_id, "conversation_id": record["id"], "checklist_id": checklist_id,
            "checklist_version": checklist["version"], "model": model, "cost_usd": cost,
            "status": status, "total": scored["total"], "original_total": original_scored["total"],
            "section_scores": scored["section_scores"], "gross_error": scored["gross_error"],
            "criteria": criteria}


def _load_evaluation(evaluation_id: str) -> tuple[dict, dict]:
    with _connect() as db:
        row = db.execute("SELECT * FROM evaluations WHERE id = ?", (evaluation_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Evaluation not found")
    return dict(row), json.loads(row["result_json"])


@router.post("/evaluations")
def create_evaluation(request: dict) -> dict:
    conversation_id = request.get("conversation_id")
    if not isinstance(conversation_id, str) or not conversation_id:
        raise HTTPException(status_code=422, detail="conversation_id is required")
    record = get_conversation(conversation_id)
    with _connect() as db:
        if request.get("checklist_id"):
            row = db.execute("SELECT * FROM checklists WHERE id = ?", (request["checklist_id"],)).fetchone()
        else:
            row = db.execute("SELECT * FROM checklists WHERE applies_to = ? AND active = 1", (record["channel"],)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Checklist not found for conversation")
    checklist = json.loads(row["checklist_json"])
    if row["applies_to"] != record["channel"]:
        raise HTTPException(status_code=422, detail="Checklist channel differs from conversation channel")
    source = json.dumps({"conversation_id": record["id"], "transcript": record["transcript"], "language": record["language"],
                         "channel": record["channel"], "checklist": checklist, "checklist_id": row["id"],
                         "models": os.environ.get("EVAL_MODELS", "")}, sort_keys=True, ensure_ascii=False)
    cache_key = hashlib.sha256(source.encode("utf-8")).hexdigest()
    with _connect() as db:
        cached = db.execute("""SELECT result_json FROM evaluations e WHERE cache_key = ? AND model != 'ERROR'
            AND NOT EXISTS (SELECT 1 FROM appeals a WHERE a.evaluation_id = e.id)
            ORDER BY rowid DESC LIMIT 1""",
                            (cache_key,)).fetchone()
    if cached:
        return json.loads(cached["result_json"])
    try:
        result, stats = _run_engine(record, checklist)
        answers = {c["id"]: c["original_answer"] for c in result["criteria"]}
        result["checklist_id"] = row["id"]
        result["checklist_version"] = row["version"]
        model = result["model"]
        duration = stats["seconds"]
        input_tokens = stats["input_tokens"]
        output_tokens = stats["output_tokens"]
        cost = result["cost_usd"]
    except (EngineEvaluationError, EngineChecklistError) as exc:
        status_code = 422 if isinstance(exc, EngineChecklistError) else 502
        result = {"id": uuid4().hex, "conversation_id": conversation_id, "checklist_id": row["id"],
                  "checklist_version": row["version"], "status": "error", "error": str(exc),
                  "model": None, "total": None, "original_total": None, "criteria": []}
        with _connect() as db:
            db.execute("""INSERT INTO evaluations (id, conversation_id, checklist_id, checklist_version,
                model, answers_json, result_json, total) VALUES (?, ?, ?, ?, 'ERROR', '{}', ?, NULL)""",
                (result["id"], conversation_id, row["id"], row["version"], json.dumps(result, ensure_ascii=False)))
        return JSONResponse(status_code=status_code, content=result)
    with _connect() as db:
        db.execute("""INSERT INTO evaluations (id, conversation_id, checklist_id, checklist_version,
            model, answers_json, result_json, total, duration_seconds, input_tokens, output_tokens,
            cost_usd, cache_key) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (result["id"], conversation_id, row["id"], row["version"], model,
             json.dumps(answers, ensure_ascii=False), json.dumps(result, ensure_ascii=False), result["total"],
             duration, input_tokens, output_tokens, cost, cache_key))
    return result


@router.get("/evaluations/{evaluation_id}")
def get_evaluation(evaluation_id: str) -> dict:
    return _load_evaluation(evaluation_id)[1]


@router.post("/evaluations/batch")
def create_batch(request: dict) -> dict:
    ids = request.get("conversation_ids")
    if not isinstance(ids, list) or not ids or any(not isinstance(cid, str) for cid in ids):
        raise HTTPException(status_code=422, detail="conversation_ids must be a nonempty string list")
    timeout = 210
    results = []
    for cid in ids:
        body = {"conversation_id": cid, "checklist_id": request.get("checklist_id")}
        last_error = None
        for attempt in range(3):
            pool = ThreadPoolExecutor(max_workers=1)
            task = pool.submit(create_evaluation, body)
            try:
                evaluation = task.result(timeout=timeout)
                if isinstance(evaluation, JSONResponse):
                    raise HTTPException(status_code=evaluation.status_code,
                                        detail=json.loads(evaluation.body).get("error", "Evaluation failed"))
                results.append({"conversation_id": cid, "evaluation": evaluation})
                last_error = None
                break
            except FutureTimeout:
                task.cancel()
                last_error = "timeout"
                break  # The timed-out worker may still be running; avoid a duplicate model call.
            except HTTPException as exc:
                last_error = exc.detail
                if exc.status_code < 500:
                    break
            except Exception as exc:
                last_error = type(exc).__name__
            finally:
                pool.shutdown(wait=False, cancel_futures=True)
            if attempt < 2:
                time.sleep(2 ** attempt)
        if last_error is not None:
            results.append({"conversation_id": cid, "error": last_error})
    return {"results": results}


@router.post("/evaluations/{evaluation_id}/appeals", status_code=201)
def create_appeal(evaluation_id: str, request: dict) -> dict:
    _, result = _load_evaluation(evaluation_id)
    if result["status"] == "error":
        raise HTTPException(status_code=409, detail="Failed evaluation cannot be appealed")
    criterion_id, comment = request.get("criterion_id"), request.get("comment")
    if not isinstance(comment, str) or not comment.strip() or criterion_id not in {c["id"] for c in result["criteria"]}:
        raise HTTPException(status_code=422, detail="Valid criterion_id and comment are required")
    appeal_id = uuid4().hex
    with _connect() as db:
        existing = db.execute("""SELECT 1 FROM appeals WHERE evaluation_id = ? AND criterion_id = ? AND status = 'open'""",
                              (evaluation_id, criterion_id)).fetchone()
        if existing:
            raise HTTPException(status_code=409, detail="Criterion already has an open appeal")
        db.execute("INSERT INTO appeals (id, evaluation_id, criterion_id, comment) VALUES (?, ?, ?, ?)",
                   (appeal_id, evaluation_id, criterion_id, comment))
        criterion = next(c for c in result["criteria"] if c["id"] == criterion_id)
        criterion["appeal"] = {"comment": comment, "status": "open", "decision": None, "reason": None}
        db.execute("UPDATE evaluations SET result_json = ? WHERE id = ?",
                   (json.dumps(result, ensure_ascii=False), evaluation_id))
    return {"id": appeal_id, "evaluation_id": evaluation_id, "criterion_id": criterion_id,
            "comment": comment, "status": "open"}


@router.get("/appeals")
def list_appeals(status: str = "open") -> list[dict]:
    with _connect() as db:
        rows = db.execute("SELECT * FROM appeals WHERE status = ? ORDER BY created_at, rowid", (status,)).fetchall()
    return [dict(row) for row in rows]


@router.post("/appeals/{appeal_id}/decision")
def decide_appeal(appeal_id: str, request: dict) -> dict:
    decision, reason = request.get("decision"), request.get("reason")
    if decision not in ("confirm", "change") or not isinstance(reason, str) or not reason.strip():
        raise HTTPException(status_code=422, detail="decision and reason are required")
    with _connect() as db:
        db.execute("BEGIN IMMEDIATE")
        appeal = db.execute("SELECT * FROM appeals WHERE id = ?", (appeal_id,)).fetchone()
        if appeal is None:
            raise HTTPException(status_code=404, detail="Appeal not found")
        if appeal["status"] != "open":
            raise HTTPException(status_code=409, detail="Appeal already decided")
        evaluation = db.execute("SELECT * FROM evaluations WHERE id = ?", (appeal["evaluation_id"],)).fetchone()
        checklist_row = db.execute("SELECT checklist_json FROM checklists WHERE id = ?", (evaluation["checklist_id"],)).fetchone()
        checklist = json.loads(checklist_row["checklist_json"])
        result = json.loads(evaluation["result_json"])
        original = json.loads(evaluation["answers_json"])
        final = {c["id"]: c["final_answer"] for c in result["criteria"]}
        if decision == "change":
            if "new_answer" not in request:
                raise HTTPException(status_code=422, detail="new_answer is required for change")
            final[appeal["criterion_id"]] = request["new_answer"]
        try:
            score(checklist, final)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        criterion = next(c for c in result["criteria"] if c["id"] == appeal["criterion_id"])
        criterion["appeal"] = {"comment": appeal["comment"], "status": "decided",
                               "decision": decision, "reason": reason}
        details = {c["id"]: {"explanation": c["explanation"], "evidence": c["evidence"]}
                   for c in result["criteria"]}
        appeals = {c["id"]: c["appeal"] for c in result["criteria"]}
        record = get_conversation(evaluation["conversation_id"])
        updated = _compose(evaluation["id"], record, evaluation["checklist_id"], checklist,
                           evaluation["model"], evaluation["cost_usd"], result["status"],
                           original, final, details, appeals)
        db.execute("INSERT INTO decisions (id, appeal_id, decision, new_answer, reason) VALUES (?, ?, ?, ?, ?)",
                   (uuid4().hex, appeal_id, decision,
                    json.dumps(request.get("new_answer")) if decision == "change" else None, reason))
        db.execute("UPDATE appeals SET status = 'decided' WHERE id = ?", (appeal_id,))
        db.execute("UPDATE evaluations SET result_json = ?, total = ? WHERE id = ?",
                   (json.dumps(updated, ensure_ascii=False), updated["total"], evaluation["id"]))
    return updated
