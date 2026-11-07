"""Export evaluations in the organizers' JSON 1.0 format.

Dialogs are evaluated by the same code and cache as the server (server/evaluations.py ->
src/cqa/evaluator.py, table `evaluations` in data/server.sqlite3), so already evaluated dialogs are not paid twice.
Results are always scored by the standard repository checklists (default-chat-vN, default-call-vN, passed to
the server as checklist_id), even if the site has another checklist active; that case only prints a warning.
By default the model's original answers are exported; appeals and reviewer decisions are ignored
(--include-decisions takes the final answers after decisions).

Needs the server dependencies (Python 3.10+): run with .venv/bin/python.

  .venv/bin/python src/cqa/export_results.py --set dev --only-cached --validate
  .venv/bin/python src/cqa/export_results.py --set prod --from-file data/raw/prod.json --validate
  .venv/bin/python src/cqa/export_results.py --set prod --from-api --group-id <id> --validate
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Optional

import scoring
from validate_results import CRITERIA_MAP, SCHEMA_VERSION, validate_file

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
DEFAULT_TEAM = "-60HZ"
EXPECT = {"dev": 65, "prod": 10}
MODE = "external_api"


class ExportError(ValueError):
    """The evaluation cannot be converted to the organizers' format."""


# ---------- pure conversion (no server, no network) ----------

def org_value(answer: Any) -> Any:
    """Our answer -> organizers' value: yes/no -> true/false, na -> null, numbers stay numbers."""
    if isinstance(answer, str):
        a = answer.strip().lower()
        if a == "yes":
            return True
        if a == "no":
            return False
        if a == "na":
            return None
        try:
            answer = float(a)
        except ValueError:
            raise ExportError(f"неизвестный ответ {answer!r}") from None
    if isinstance(answer, bool) or not isinstance(answer, (int, float)):
        raise ExportError(f"неизвестный ответ {answer!r}")
    return int(answer) if float(answer).is_integer() else float(answer)


def evidence_turns(evidence: list) -> list[int]:
    """Our message_index (from 0) -> organizers' turn number (from 1), in order, without repeats."""
    turns: list[int] = []
    for ev in evidence or []:
        idx = ev.get("message_index") if isinstance(ev, dict) else None
        if isinstance(idx, int) and not isinstance(idx, bool) and idx >= 0 and idx + 1 not in turns:
            turns.append(idx + 1)
    return turns


def ok_record(conversation_id: str, checklist: dict, answers: dict, details: dict) -> dict:
    """One status=ok record. Total comes from scoring.score (N/A excluded, half-up to 2 digits)."""
    ours = {c["id"] for c in checklist["criteria"]}
    unmapped = ours - {o for o, _ in CRITERIA_MAP}
    if unmapped:
        raise ExportError(f"критерии чек-листа {sorted(unmapped)} не сопоставлены с форматом организаторов")
    criteria = []
    for our_id, org_id in CRITERIA_MAP:
        if our_id not in answers:
            raise ExportError(f"в оценке нет критерия {our_id}")
        detail = details.get(our_id) or {}
        criteria.append({"criterion_id": org_id, "value": org_value(answers[our_id]),
                         "rationale": str(detail.get("explanation") or "").strip(),
                         "evidence_turns": evidence_turns(detail.get("evidence"))})
    total = scoring.score(checklist, answers)["total"]
    critical = org_value(answers["gross_errors"])
    return {"conversation_id": conversation_id, "status": "ok", "criteria": criteria,
            "score": {"final_score": total, "critical_flag": bool(critical)}}


def error_record(conversation_id: str, reason: str) -> dict:
    return {"conversation_id": conversation_id, "status": "error", "criteria": [], "score": None,
            "error": str(reason).strip() or "неизвестная ошибка"}


def build_document(team_name: str, models: list[str], results: list[dict], fallback_model: str) -> dict:
    names = list(dict.fromkeys(m for m in models if m))  # unique, in order of first use
    return {"schema_version": SCHEMA_VERSION, "team_name": team_name,
            "model": {"mode": MODE, "name": ", ".join(names) or fallback_model},
            "results": results}


def _norm(s: str) -> str:
    return " ".join(str(s).split()).lower()


def check_evidence(items: list[dict], dialogs: dict[str, dict]) -> dict:
    """Check that every quote is in transcript[turn - 1] of the source file.

    items: [{"conversation_id", "details": {criterion: {"evidence": [{"message_index", "quote"}]}}}]
    Returns {"total", "matched", "mismatches": [...]} with a reason for each mismatch.
    """
    total = matched = 0
    mismatches = []
    for it in items:
        tr = dialogs[it["conversation_id"]]["transcript"]
        for crit, det in sorted((it.get("details") or {}).items()):
            for ev in det.get("evidence") or []:
                idx, quote = ev.get("message_index"), str(ev.get("quote") or "").strip()
                if not isinstance(idx, int) or idx < 0:
                    continue
                total += 1
                turn = idx + 1
                base = {"conversation_id": it["conversation_id"], "criterion": crit, "turn": turn, "quote": quote[:60]}
                if idx >= len(tr):
                    mismatches.append({**base, "reason": f"реплики {turn} нет (в диалоге {len(tr)})"})
                    continue
                text = tr[turn - 1].get("text", "")
                base["speaker"] = tr[turn - 1].get("speaker")
                if not quote:
                    mismatches.append({**base, "reason": "пустая цитата"})
                elif _norm(quote) in _norm(text):
                    matched += 1
                else:
                    parts = [p for p in re.split(r"\.\.\.|…", quote) if p.strip()]
                    elsewhere = [i + 1 for i, m in enumerate(tr) if _norm(quote) in _norm(m.get("text", ""))]
                    if len(parts) > 1 and all(_norm(p) in _norm(text) for p in parts):
                        reason = "цитата сокращена многоточием, все части есть в реплике"
                    elif elsewhere:
                        reason = f"цитата есть в другой реплике: {elsewhere}"
                    else:
                        reason = "цитаты нет в реплике (перефразирована?)"
                    mismatches.append({**base, "reason": reason})
    return {"total": total, "matched": matched, "mismatches": mismatches}


# ---------- server, cache and evaluation ----------

def _server():
    """Import the server modules (same DB, checklists and evaluation code as the running server)."""
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    try:
        import server.app as app
        import server.evaluations as evaluations
    except Exception as e:  # noqa: BLE001 - explain how to run instead of a traceback
        sys.exit(f"Не удалось загрузить код сервера ({type(e).__name__}: {e}).\n"
                 "Запускайте через .venv/bin/python (Python 3.10+, зависимости из server/requirements.txt).")
    app._init_db()
    return app, evaluations


def server_cache_key(record: dict, row) -> str:
    """Exactly the key server/evaluations.py create_evaluation uses."""
    checklist = json.loads(row["checklist_json"])
    source = json.dumps({"conversation_id": record["id"], "transcript": record["transcript"], "language": record["language"],
                         "channel": record["channel"], "checklist": checklist, "checklist_id": row["id"],
                         "models": os.environ.get("EVAL_MODELS", "")}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def _active_checklist(app, channel: str):
    with app._connect() as db:
        return db.execute("SELECT * FROM checklists WHERE applies_to = ? AND active = 1", (channel,)).fetchone()


# Standard checklists: the repository files, stored by the server under these ids (server/app.py _init_db).
STANDARD_FILES = (("checklist.default.json", "default-chat"), ("checklist.calls.json", "default-call"))


def standard_checklists() -> dict[str, tuple[str, dict]]:
    """channel -> (server id like default-chat-v3, checklist from the repository file)."""
    out = {}
    for filename, prefix in STANDARD_FILES:
        ck = json.loads((HERE / filename).read_text(encoding="utf-8"))
        out[ck["applies_to"]] = (f"{prefix}-v{ck['version']}", ck)
    return out


def _standard_checklist(app, channel: str):
    """The server's row of the standard checklist for the channel (results are always scored by it)."""
    std = standard_checklists().get(channel)
    if std is None:
        raise ExportError(f"нет стандартного чек-листа для канала {channel}")
    with app._connect() as db:
        row = db.execute("SELECT * FROM checklists WHERE id = ?", (std[0],)).fetchone()
    if row is None:
        raise ExportError(f"в базе сервера нет стандартного чек-листа {std[0]}")
    return row


def checklist_warnings(app) -> list[str]:
    """Warn (do not fail) if the site uses another checklist or the stored standard differs from the file."""
    warnings = []
    for channel, (std_id, file_ck) in standard_checklists().items():
        active = _active_checklist(app, channel)
        if active is not None and active["id"] != std_id:
            warnings.append(f"на сайте для канала «{channel}» активен чек-лист {active['id']} (версия {active['version']}), "
                            f"а results.json считается по стандартному {std_id}")
        try:
            row = _standard_checklist(app, channel)
        except ExportError as e:
            warnings.append(str(e))
            continue
        if json.loads(row["checklist_json"]) != file_ck:
            warnings.append(f"{std_id} в базе сервера отличается от файла репозитория той же версии; "
                            "используется версия из базы (её же видит сервер)")
    return warnings


def _cached_row(app, key: str):
    """Latest successful evaluation with this key, including ones that later got appeals."""
    with app._connect() as db:
        return db.execute("""SELECT * FROM evaluations WHERE cache_key = ? AND model != 'ERROR'
            ORDER BY rowid DESC LIMIT 1""", (key,)).fetchone()


def _from_row(app, row, include_decisions: bool) -> tuple[dict, dict, dict]:
    result = json.loads(row["result_json"])
    with app._connect() as db:
        ck_row = db.execute("SELECT checklist_json FROM checklists WHERE id = ?", (row["checklist_id"],)).fetchone()
    checklist = json.loads(ck_row["checklist_json"])
    if include_decisions:
        answers = {c["id"]: c["final_answer"] for c in result["criteria"]}
    else:
        answers = json.loads(row["answers_json"])  # the model's original answers
    details = {c["id"]: {"explanation": c.get("explanation", ""), "evidence": c.get("evidence", [])}
               for c in result["criteria"]}
    return checklist, answers, details


def _evaluate(evaluations, conversation_id: str, checklist_id: str) -> Optional[str]:
    """Run the server's evaluation with the given (standard) checklist; return an error text or None."""
    try:
        out = evaluations.create_evaluation({"conversation_id": conversation_id, "checklist_id": checklist_id})
    except Exception as e:  # noqa: BLE001 - one dialog must not stop the export
        detail = getattr(e, "detail", None)
        return str(detail or f"{type(e).__name__}: {e}")
    if hasattr(out, "body"):  # JSONResponse with status=error
        try:
            return json.loads(out.body).get("error") or "ошибка оценки"
        except ValueError:
            return "ошибка оценки"
    return None if isinstance(out, dict) and out.get("status") != "error" else (out.get("error") or "ошибка оценки")


# ---------- sources ----------

def load_source(args) -> tuple[list[dict], Path]:
    if args.from_file:
        path = Path(args.from_file)
    elif args.from_api:
        import fetch_data
        import llm
        url = llm.load_env().get("DATA_API_URL")
        if not url:
            sys.exit("DATA_API_URL в .env пустой")
        payload = fetch_data.fetch(fetch_data.with_group_id(url, args.group_id))
        if not payload.get("isSuccess") or not isinstance((payload.get("data") or {}).get("records"), list):
            sys.exit("API вернул неожиданный ответ (нет isSuccess или data.records)")
        path = ROOT / "data" / "raw" / f"{args.set}_api.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"диалоги из API сохранены: {path.relative_to(ROOT)}")
    elif args.set == "dev" and (ROOT / "data" / "raw" / "dev.json").exists():
        path = ROOT / "data" / "raw" / "dev.json"
    else:
        sys.exit("Укажите источник диалогов: --from-file путь.json или --from-api [--group-id ...]")
    records = json.loads(path.read_text(encoding="utf-8"))["data"]["records"]
    if args.ids:
        wanted = set(args.ids)
        records = [r for r in records if r.get("conversation_id") in wanted]
        missing = wanted - {r.get("conversation_id") for r in records}
        if missing:
            sys.exit(f"Нет в источнике: {', '.join(sorted(missing))}")
    return records, path


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--set", choices=["dev", "prod"], required=True)
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--from-file", help="dialogs file in data/raw/dev.json format")
    src.add_argument("--from-api", action="store_true", help="download dialogs by DATA_API_URL from .env")
    ap.add_argument("--group-id", help="group_id for --from-api (e.g. the prod group)")
    ap.add_argument("--out", help="output file (default results/<set>/results.json)")
    ap.add_argument("--ids", nargs="+", help="only these conversation ids")
    ap.add_argument("--only-cached", action="store_true", help="do not evaluate: stop and list what is not cached")
    ap.add_argument("--workers", type=int, default=4, help="parallel evaluations (default 4)")
    ap.add_argument("--include-decisions", action="store_true",
                    help="use answers after reviewers' decisions (default: model's original answers)")
    ap.add_argument("--check-evidence", action="store_true", help="check that quotes are in transcript[turn-1]")
    ap.add_argument("--validate", action="store_true", help="validate the written file")
    args = ap.parse_args()

    records, source_path = load_source(args)
    app, evaluations = _server()
    team = os.environ.get("TEAM_NAME", "").strip() or DEFAULT_TEAM
    errors: dict[str, str] = {}
    plan: dict[str, Optional[str]] = {}  # conversation_id -> cache key
    checklist_of: dict[str, str] = {}    # conversation_id -> standard checklist id
    used_checklists = set()
    for w in checklist_warnings(app):
        print("ВНИМАНИЕ:", w)
    for rec in records:
        cid = str(rec.get("conversation_id") or "")
        try:
            app.create_conversation(rec)  # same storage the server uses (upsert)
            db_rec = app.get_conversation(cid)
        except Exception as e:  # noqa: BLE001
            errors[cid] = f"некорректный диалог: {getattr(e, 'detail', None) or e}"
            plan[cid] = None
            continue
        try:
            row = _standard_checklist(app, db_rec["channel"])
        except ExportError as e:
            errors[cid] = str(e)
            plan[cid] = None
            continue
        used_checklists.add(f"{row['id']} ({row['applies_to']})")
        checklist_of[cid] = row["id"]
        plan[cid] = server_cache_key(db_rec, row)
    print(f"диалогов: {len(records)}; чек-листы выгрузки (стандартные): {', '.join(sorted(used_checklists))}")

    missing = [cid for cid, key in plan.items() if key and _cached_row(app, key) is None]
    print(f"в кэше: {len(plan) - len(missing) - len(errors)}, нужно оценить: {len(missing)}")
    if args.only_cached and missing:
        print(f"Остановлено (--only-cached): нет в кэше {len(missing)} диалогов:")
        print("  " + ", ".join(missing))
        sys.exit(2)

    if missing:
        started = time.time()

        def work(cid):
            t = time.time()
            err = _evaluate(evaluations, cid, checklist_of[cid])
            print(f"  {cid:14} {'ok' if err is None else 'ошибка: ' + err[:80]} ({time.time() - t:.1f} с)", flush=True)
            return cid, err

        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
            for cid, err in pool.map(work, missing):
                if err:
                    errors[cid] = err
        print(f"оценено за {time.time() - started:.0f} с")

    results, models, items, new_cost = [], [], [], 0.0
    for rec in records:
        cid = str(rec.get("conversation_id") or "")
        if cid in errors:
            results.append(error_record(cid, errors[cid]))
            continue
        row = _cached_row(app, plan[cid])
        if row is None:
            results.append(error_record(cid, "оценка не найдена после запуска"))
            continue
        try:
            checklist, answers, details = _from_row(app, row, args.include_decisions)
            results.append(ok_record(cid, checklist, answers, details))
            models.append(row["model"])
            items.append({"conversation_id": cid, "details": details})
            if cid in missing and row["cost_usd"]:
                new_cost += row["cost_usd"]
        except (ExportError, ValueError, KeyError) as e:
            results.append(error_record(cid, f"не удалось выгрузить оценку: {e}"))

    doc = build_document(team, models, results, os.environ.get("EVAL_MODELS", "") or "unknown")
    out = Path(args.out) if args.out else ROOT / "results" / args.set / "results.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    n_err = sum(r["status"] == "error" for r in results)
    print(f"записано: {out} — записей {len(results)}, ok {len(results) - n_err}, error {n_err}; "
          f"модели: {doc['model']['name']}; ответы: {'после решений' if args.include_decisions else 'исходные модели'}"
          + (f"; стоимость новых оценок ${new_cost:.4f}" if missing else ""))

    if args.check_evidence:
        dialogs = {str(r["conversation_id"]): r for r in records}
        rep = check_evidence(items, dialogs)
        print(f"цитаты: совпало {rep['matched']} из {rep['total']}")
        for m in rep["mismatches"]:
            print(f"  {m['conversation_id']} {m['criterion']} реплика {m['turn']} ({m.get('speaker', '?')}): {m['reason']}")
    if args.validate:
        expect = len(records) if args.ids else EXPECT.get(args.set)
        sys.exit(validate_file(out, source_path, expect))


if __name__ == "__main__":
    main()
