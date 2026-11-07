"""Evaluation, batch processing, and appeal API."""
from __future__ import annotations

import hashlib
import importlib
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from pathlib import Path
from typing import Optional
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from src.cqa.scoring import score
router = APIRouter(prefix="/api")
ROOT = Path(__file__).resolve().parents[1]
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()


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
             model: str, cost: Optional[float], status: str, original: dict,
             final: dict, details: dict, appeals: Optional[dict] = None) -> dict:
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
        raise HTTPException(status_code=404, detail="Оценка не найдена")
    return dict(row), json.loads(row["result_json"])


@router.post("/evaluations")
def create_evaluation(request: dict) -> dict:
    supplied = request.get("conversation") or request.get("record")
    if supplied is None and "transcript" in request:
        supplied = request
    if supplied is not None:
        if not isinstance(supplied, dict):
            raise HTTPException(status_code=422, detail="Диалог должен быть объектом JSON")
        importlib.import_module("server.app").create_conversation(supplied)
        request = {**request, "conversation_id": supplied.get("conversation_id") or supplied.get("id")}
    conversation_id = request.get("conversation_id")
    if not isinstance(conversation_id, str) or not conversation_id:
        raise HTTPException(status_code=422, detail="Укажите conversation_id")
    record = get_conversation(conversation_id)
    with _connect() as db:
        if request.get("checklist_id"):
            row = db.execute("SELECT * FROM checklists WHERE id = ?", (request["checklist_id"],)).fetchone()
        else:
            row = db.execute("SELECT * FROM checklists WHERE applies_to = ? AND active = 1", (record["channel"],)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Чек-лист для диалога не найден")
    checklist = json.loads(row["checklist_json"])
    if row["applies_to"] != record["channel"]:
        raise HTTPException(status_code=422, detail="Канал чек-листа не совпадает с каналом диалога")
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
                  "checklist_version": row["version"], "status": "error", "error": f"Ошибка оценки: {exc}",
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


@router.get("/evaluations")
def list_evaluations(conversation_id: Optional[str] = None) -> list[dict]:
    with _connect() as db:
        if conversation_id:
            rows = db.execute("SELECT result_json FROM evaluations WHERE conversation_id = ? ORDER BY created_at, rowid",
                              (conversation_id,)).fetchall()
        else:
            rows = db.execute("SELECT result_json FROM evaluations ORDER BY created_at DESC, rowid DESC LIMIT 100").fetchall()
    return [json.loads(row["result_json"]) for row in rows]


@router.post("/evaluations/batch")
def create_batch(request: dict) -> dict:
    ids = request.get("conversation_ids")
    if not isinstance(ids, list) or not ids or any(not isinstance(cid, str) for cid in ids):
        raise HTTPException(status_code=422, detail="conversation_ids должен быть непустым списком ID")
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
                                        detail=json.loads(evaluation.body).get("error", "Ошибка оценки"))
                results.append({"conversation_id": cid, "evaluation": evaluation})
                last_error = None
                break
            except FutureTimeout:
                task.cancel()
                last_error = "Превышено время ожидания оценки"
                break  # The timed-out worker may still be running; avoid a duplicate model call.
            except HTTPException as exc:
                last_error = exc.detail
                if exc.status_code < 500:
                    break
            except Exception as exc:
                last_error = "Внутренняя ошибка при оценке диалога"
            finally:
                pool.shutdown(wait=False, cancel_futures=True)
            if attempt < 2:
                time.sleep(2 ** attempt)
        if last_error is not None:
            results.append({"conversation_id": cid, "status": "error", "error": last_error})
    return {"status": "done", "completed": len(results), "total": len(ids), "results": results}


def _run_batch_job(job_id: str, ids: list[str], checklist_id: Optional[str]) -> None:
    for cid in ids:
        result = create_batch({"conversation_ids": [cid], "checklist_id": checklist_id})["results"][0]
        with _jobs_lock:
            job = _jobs[job_id]
            job["results"].append(result)
            job["completed"] += 1
    with _jobs_lock:
        _jobs[job_id]["status"] = "done"


def start_batch_job(ids: list[str], checklist_id: Optional[str]) -> dict:
    if not ids:
        raise HTTPException(status_code=422, detail="Нет диалогов для пакетной оценки")
    job_id = uuid4().hex
    job = {"id": job_id, "status": "running", "completed": 0, "total": len(ids), "results": []}
    with _jobs_lock:
        _jobs[job_id] = job
    threading.Thread(target=_run_batch_job, args=(job_id, ids, checklist_id), daemon=True).start()
    return dict(job)


@router.post("/evaluations/batch/start", status_code=202)
def start_batch(request: dict) -> dict:
    ids = request.get("conversation_ids")
    if not isinstance(ids, list) or any(not isinstance(cid, str) for cid in ids):
        raise HTTPException(status_code=422, detail="conversation_ids должен быть списком ID")
    return start_batch_job(ids, request.get("checklist_id"))


@router.get("/evaluations/batch/{job_id}")
def batch_progress(job_id: str) -> dict:
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Пакетная задача не найдена")
        return {**job, "results": list(job["results"])}


@router.post("/evaluations/{evaluation_id}/appeals", status_code=201)
def create_appeal(evaluation_id: str, request: dict) -> dict:
    _, result = _load_evaluation(evaluation_id)
    if result["status"] == "error":
        raise HTTPException(status_code=409, detail="Нельзя подать апелляцию на неудавшуюся оценку")
    criterion_id, comment = request.get("criterion_id"), request.get("comment")
    if not isinstance(comment, str) or not comment.strip() or criterion_id not in {c["id"] for c in result["criteria"]}:
        raise HTTPException(status_code=422, detail="Укажите критерий и комментарий к апелляции")
    appeal_id = uuid4().hex
    with _connect() as db:
        existing = db.execute("""SELECT 1 FROM appeals WHERE evaluation_id = ? AND criterion_id = ? AND status = 'open'""",
                              (evaluation_id, criterion_id)).fetchone()
        if existing:
            raise HTTPException(status_code=409, detail="По критерию уже есть открытая апелляция")
        db.execute("INSERT INTO appeals (id, evaluation_id, criterion_id, comment) VALUES (?, ?, ?, ?)",
                   (appeal_id, evaluation_id, criterion_id, comment))
        criterion = next(c for c in result["criteria"] if c["id"] == criterion_id)
        criterion["appeal"] = {"comment": comment, "status": "open", "decision": None, "reason": None}
        db.execute("UPDATE evaluations SET result_json = ? WHERE id = ?",
                   (json.dumps(result, ensure_ascii=False), evaluation_id))
    return {"id": appeal_id, "evaluation_id": evaluation_id, "criterion_id": criterion_id,
            "comment": comment, "status": "open"}


@router.get("/appeals")
def list_appeals(status: Optional[str] = None) -> list[dict]:
    if status not in (None, "open", "decided"):
        raise HTTPException(status_code=422, detail="Неизвестный статус апелляции")
    with _connect() as db:
        rows = db.execute("""SELECT a.*, e.conversation_id, e.checklist_version, e.result_json,
            d.decision, d.new_answer, d.reason
            FROM appeals a
            JOIN evaluations e ON e.id = a.evaluation_id
            LEFT JOIN decisions d ON d.appeal_id = a.id
            WHERE (? IS NULL OR a.status = ?)
            ORDER BY a.created_at, a.rowid""", (status, status)).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        evaluation = json.loads(item.pop("result_json"))
        criterion = next((c for c in evaluation.get("criteria", []) if c["id"] == item["criterion_id"]), {})
        item["criterion_title"] = criterion.get("title", item["criterion_id"])
        item["final_answer"] = criterion.get("final_answer", criterion.get("answer"))
        if item["new_answer"] is not None:
            item["new_answer"] = json.loads(item["new_answer"])
        result.append(item)
    return result


@router.post("/appeals/{appeal_id}/decision")
def decide_appeal(appeal_id: str, request: dict) -> dict:
    decision, reason = request.get("decision"), request.get("reason")
    if decision not in ("confirm", "change") or not isinstance(reason, str) or not reason.strip():
        raise HTTPException(status_code=422, detail="Укажите решение и его причину")
    with _connect() as db:
        db.execute("BEGIN IMMEDIATE")
        appeal = db.execute("SELECT * FROM appeals WHERE id = ?", (appeal_id,)).fetchone()
        if appeal is None:
            raise HTTPException(status_code=404, detail="Апелляция не найдена")
        if appeal["status"] != "open":
            raise HTTPException(status_code=409, detail="Решение по апелляции уже принято")
        evaluation = db.execute("SELECT * FROM evaluations WHERE id = ?", (appeal["evaluation_id"],)).fetchone()
        checklist_row = db.execute("SELECT checklist_json FROM checklists WHERE id = ?", (evaluation["checklist_id"],)).fetchone()
        checklist = json.loads(checklist_row["checklist_json"])
        result = json.loads(evaluation["result_json"])
        original = json.loads(evaluation["answers_json"])
        final = {c["id"]: c["final_answer"] for c in result["criteria"]}
        if decision == "change":
            if "new_answer" not in request:
                raise HTTPException(status_code=422, detail="Для изменения укажите новый ответ")
            final[appeal["criterion_id"]] = request["new_answer"]
        try:
            score(checklist, final)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=f"Недопустимый ответ для критерия: {exc}") from None
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
