"""FastAPI entry point and persistent DEV conversation import."""
from __future__ import annotations

import json
import importlib
import os
import ssl
import sqlite3
from uuid import uuid4
from html import escape
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
import certifi

from src.cqa.scoring import validate_checklist

ROOT = Path(__file__).resolve().parents[1]


def _load_env() -> None:
    path = ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_env()
DB_PATH = Path(os.environ.get("CQA_DB_PATH", str(ROOT / "data" / "server.sqlite3")))
DEV_PATH = ROOT / "data" / "raw" / "dev.json"


@contextmanager
def _connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def _init_db() -> None:
    with _connect() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS conversations (
            id TEXT PRIMARY KEY,
            channel TEXT NOT NULL,
            language TEXT NOT NULL,
            transcript TEXT NOT NULL,
            audio_url TEXT
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS checklists (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            version INTEGER NOT NULL,
            applies_to TEXT NOT NULL CHECK (applies_to IN ('agent chat', 'voice call')),
            checklist_json TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 0 CHECK (active IN (0, 1)),
            UNIQUE (name, applies_to, version)
        )""")
        db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS one_active_checklist_per_channel
            ON checklists(applies_to) WHERE active = 1""")
        db.execute("""CREATE TABLE IF NOT EXISTS evaluations (
            id TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL,
            checklist_id TEXT NOT NULL REFERENCES checklists(id),
            checklist_version INTEGER NOT NULL,
            model TEXT NOT NULL,
            answers_json TEXT NOT NULL,
            result_json TEXT NOT NULL,
            total REAL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            duration_seconds REAL,
            input_tokens INTEGER,
            output_tokens INTEGER,
            cost_usd REAL
        )""")
        columns = {row[1] for row in db.execute("PRAGMA table_info(evaluations)")}
        if "cache_key" not in columns:
            db.execute("ALTER TABLE evaluations ADD COLUMN cache_key TEXT")
        db.execute("CREATE INDEX IF NOT EXISTS evaluation_cache_key ON evaluations(cache_key)")
        db.execute("""CREATE TABLE IF NOT EXISTS appeals (
            id TEXT PRIMARY KEY,
            evaluation_id TEXT NOT NULL REFERENCES evaluations(id),
            criterion_id TEXT NOT NULL,
            comment TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS decisions (
            id TEXT PRIMARY KEY,
            appeal_id TEXT NOT NULL UNIQUE REFERENCES appeals(id),
            decision TEXT NOT NULL,
            new_answer TEXT,
            reason TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""")
        for filename, seed_prefix in (("checklist.default.json", "default-chat"),
                                      ("checklist.calls.json", "default-call")):
            checklist = json.loads((ROOT / "src" / "cqa" / filename).read_text(encoding="utf-8"))
            validate_checklist(checklist)
            channel = checklist["applies_to"]
            seed_id = f"{seed_prefix}-v{checklist['version']}"
            active_row = db.execute("SELECT id FROM checklists WHERE applies_to = ? AND active = 1", (channel,)).fetchone()
            if active_row and active_row["id"].startswith(seed_prefix + "-v") and active_row["id"] != seed_id:
                db.execute("UPDATE checklists SET active = 0 WHERE id = ?", (active_row["id"],))
                active_row = None
            db.execute("""INSERT OR IGNORE INTO checklists
                (id, name, version, applies_to, checklist_json, active) VALUES (?, ?, ?, ?, ?, ?)""",
                (seed_id, checklist["name"], checklist["version"], channel,
                 json.dumps(checklist, ensure_ascii=False), 0 if active_row else 1))
            if active_row is None:
                db.execute("UPDATE checklists SET active = 1 WHERE id = ?", (seed_id,))


def _checklist_response(row: sqlite3.Row, include_json: bool = False) -> dict:
    result = {"id": row["id"], "name": row["name"], "version": row["version"],
              "applies_to": row["applies_to"], "active": bool(row["active"])}
    if include_json:
        result.update(json.loads(row["checklist_json"]))
    return result


def import_conversations() -> int:
    """Import the private DEV file or fetch the DEV API server-side."""
    url = os.environ.get("DATA_API_URL", "").strip()
    if DEV_PATH.exists():
        payload = json.loads(DEV_PATH.read_text(encoding="utf-8"))
    elif url:
        request = Request(url, headers={"Accept": "application/json", "User-Agent": "CQA-server/1.0"})
        context = ssl.create_default_context(cafile=certifi.where())
        with urlopen(request, timeout=20, context=context) as response:
            payload = json.load(response)
    else:
        return 0
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(payload, dict) or payload.get("isSuccess") is not True or not isinstance(data, dict) or not isinstance(data.get("records"), list):
        raise ValueError("Data API returned an invalid response")
    records = []
    for record in data["records"]:
        if not isinstance(record, dict) or not record.get("conversation_id") or not isinstance(record.get("transcript"), list):
            raise ValueError("Data API record is missing conversation_id or transcript")
        records.append((str(record["conversation_id"]), record.get("channel", ""),
                        record.get("language", ""), json.dumps(record["transcript"], ensure_ascii=False),
                        record.get("audio_url")))
    with _connect() as db:
        db.executemany("""INSERT INTO conversations (id, channel, language, transcript, audio_url)
            VALUES (?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET
            channel=excluded.channel, language=excluded.language,
            transcript=excluded.transcript, audio_url=excluded.audio_url""", records)
    return len(records)


@asynccontextmanager
async def lifespan(_: FastAPI):
    _init_db()
    if DEV_PATH.exists() or os.environ.get("DATA_API_URL", "").strip():
        try:
            import_conversations()
        except (HTTPError, URLError, TimeoutError, ValueError, OSError) as exc:
            # Existing cached conversations remain available during an upstream outage.
            print(f"Data API import failed: {type(exc).__name__}")
    yield


app = FastAPI(title="CQA Server", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(StarletteHTTPException)
async def http_error_handler(_, exc: StarletteHTTPException):
    detail = "Ресурс не найден" if exc.status_code == 404 and exc.detail == "Not Found" else exc.detail
    return JSONResponse(status_code=exc.status_code, content={"status": "error", "detail": detail})


@app.exception_handler(RequestValidationError)
async def validation_error_handler(_, __: RequestValidationError):
    return JSONResponse(status_code=422, content={"status": "error", "detail": "Неверный формат запроса"})


@app.exception_handler(Exception)
async def unexpected_error_handler(_, __: Exception):
    return JSONResponse(status_code=500, content={"status": "error", "detail": "Внутренняя ошибка сервера"})


@app.get("/api/health")
def health() -> dict:
    with _connect() as db:
        db.execute("SELECT 1")
    return {"status": "ok"}


@app.get("/api/checklists")
def list_checklists() -> list[dict]:
    with _connect() as db:
        rows = db.execute("SELECT * FROM checklists ORDER BY applies_to, name, version DESC").fetchall()
    return [_checklist_response(row) for row in rows]


@app.get("/api/checklists/{checklist_id}")
def get_checklist(checklist_id: str) -> dict:
    with _connect() as db:
        row = db.execute("SELECT * FROM checklists WHERE id = ?", (checklist_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Чек-лист не найден")
    return _checklist_response(row, include_json=True)


@app.post("/api/checklists", status_code=201)
def create_checklist(checklist: dict) -> dict:
    """Save an immutable version; activate it with the separate activate endpoint."""
    channel = checklist.get("applies_to")
    name = checklist.get("name")
    if channel not in ("agent chat", "voice call") or not isinstance(name, str) or not name.strip():
        raise HTTPException(status_code=422, detail="Укажите название и допустимый канал чек-листа")
    try:
        validate_checklist(checklist)
    except (ValueError, KeyError, TypeError) as exc:
        raise HTTPException(status_code=422, detail=f"Неверный чек-лист: {exc}") from None
    new_id = uuid4().hex
    with _connect() as db:
        db.execute("BEGIN IMMEDIATE")
        base_id = checklist.get("id") or checklist.get("base_id")
        if base_id:
            base = db.execute("SELECT name, applies_to FROM checklists WHERE id = ?", (base_id,)).fetchone()
            if base is None:
                raise HTTPException(status_code=404, detail="Исходный чек-лист не найден")
            if base["name"] != name or base["applies_to"] != channel:
                raise HTTPException(status_code=422, detail="Название или канал исходного чек-листа не совпадает")
        version = db.execute("SELECT COALESCE(MAX(version), 0) + 1 FROM checklists WHERE name = ? AND applies_to = ?",
                             (name, channel)).fetchone()[0]
        stored = {k: v for k, v in checklist.items() if k not in ("id", "base_id", "active")}
        stored["version"] = version
        active = 0 if db.execute("SELECT 1 FROM checklists WHERE applies_to = ? AND active = 1", (channel,)).fetchone() else 1
        db.execute("""INSERT INTO checklists (id, name, version, applies_to, checklist_json, active)
            VALUES (?, ?, ?, ?, ?, ?)""",
            (new_id, name, version, channel, json.dumps(stored, ensure_ascii=False), active))
        row = db.execute("SELECT * FROM checklists WHERE id = ?", (new_id,)).fetchone()
    return _checklist_response(row, include_json=True)


@app.post("/api/checklists/{checklist_id}/activate")
def activate_checklist(checklist_id: str) -> dict:
    with _connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM checklists WHERE id = ?", (checklist_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Чек-лист не найден")
        db.execute("UPDATE checklists SET active = 0 WHERE applies_to = ?", (row["applies_to"],))
        db.execute("UPDATE checklists SET active = 1 WHERE id = ?", (checklist_id,))
        row = db.execute("SELECT * FROM checklists WHERE id = ?", (checklist_id,)).fetchone()
    return _checklist_response(row)


@app.post("/api/checklists/{checklist_id}/recalculate", status_code=202)
def recalculate_checklist(checklist_id: str) -> dict:
    with _connect() as db:
        checklist = db.execute("SELECT applies_to FROM checklists WHERE id = ?", (checklist_id,)).fetchone()
        if checklist is None:
            raise HTTPException(status_code=404, detail="Чек-лист не найден")
        rows = db.execute("SELECT id FROM conversations WHERE channel = ? ORDER BY id",
                          (checklist["applies_to"],)).fetchall()
    ids = [row["id"] for row in rows]
    return importlib.import_module("server.evaluations").start_batch_job(ids, checklist_id)


@app.get("/api/conversations")
def list_conversations(gross_error: bool | None = None) -> list[dict]:
    with _connect() as db:
        rows = db.execute("""SELECT c.id, c.channel, c.language,
            (SELECT e.total FROM evaluations e WHERE e.conversation_id = c.id
             ORDER BY e.created_at DESC, e.rowid DESC LIMIT 1) AS total,
            (SELECT e.id FROM evaluations e WHERE e.conversation_id = c.id
             ORDER BY e.created_at DESC, e.rowid DESC LIMIT 1) AS last_evaluation_id,
            (SELECT e.result_json FROM evaluations e WHERE e.conversation_id = c.id
             ORDER BY e.created_at DESC, e.rowid DESC LIMIT 1) AS last_result,
            EXISTS (SELECT 1 FROM appeals a JOIN evaluations e ON e.id = a.evaluation_id
                    WHERE e.conversation_id = c.id AND a.status = 'open') AS has_appeal
            FROM conversations c ORDER BY c.id""").fetchall()
    result = []
    for row in rows:
        evaluation = json.loads(row["last_result"]) if row["last_result"] else None
        item = {"id": row["id"], "channel": row["channel"], "language": row["language"],
                "total": row["total"], "last_evaluation_id": row["last_evaluation_id"],
                "gross_error": bool(evaluation and evaluation.get("gross_error")),
                "has_appeal": bool(row["has_appeal"]),
                "status": evaluation.get("status") if evaluation else None}
        if gross_error is None or item["gross_error"] == gross_error:
            result.append(item)
    return result


@app.post("/api/conversations", status_code=201)
def create_conversation(record: dict) -> dict:
    conversation_id = record.get("conversation_id") or record.get("id")
    channel = record.get("channel")
    language = record.get("language")
    transcript = record.get("transcript")
    if not isinstance(conversation_id, str) or not conversation_id.strip():
        raise HTTPException(status_code=422, detail="Укажите conversation_id диалога")
    if channel not in ("agent chat", "voice call"):
        raise HTTPException(status_code=422, detail="Укажите канал: agent chat или voice call")
    if language not in ("RU", "KZ", "EN"):
        raise HTTPException(status_code=422, detail="Укажите язык: RU, KZ или EN")
    if not isinstance(transcript, list) or not transcript or any(
        not isinstance(m, dict) or not isinstance(m.get("speaker"), str)
        or not isinstance(m.get("text"), str) for m in transcript
    ):
        raise HTTPException(status_code=422, detail="Транскрипт должен содержать реплики speaker и text")
    with _connect() as db:
        db.execute("""INSERT INTO conversations (id, channel, language, transcript, audio_url)
            VALUES (?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET
            channel=excluded.channel, language=excluded.language,
            transcript=excluded.transcript, audio_url=excluded.audio_url""",
            (conversation_id, channel, language, json.dumps(transcript, ensure_ascii=False), record.get("audio_url")))
    return get_conversation(conversation_id)


@app.get("/api/conversations/{conversation_id}")
def get_conversation(conversation_id: str) -> dict:
    with _connect() as db:
        row = db.execute("SELECT * FROM conversations WHERE id = ?", (conversation_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Диалог не найден")
    return {"id": row["id"], "channel": row["channel"], "language": row["language"],
            "transcript": json.loads(row["transcript"]), "audio_url": row["audio_url"]}


@app.get("/api/conversations/{conversation_id}/view", response_class=HTMLResponse)
def view_conversation(conversation_id: str) -> str:
    """Readable, numbered transcript for a human labeler."""
    record = get_conversation(conversation_id)
    heading = escape(conversation_id)
    rows = []
    for index, message in enumerate(record["transcript"]):
        timestamp = escape(str(message.get("timestamp", "")))
        speaker = escape(str(message.get("speaker", "")))
        body = escape(str(message.get("text", "")))
        rows.append(f'<article><strong>#{index} · {timestamp} · {speaker}</strong><p>{body}</p></article>')
    return ("<!doctype html><html lang='ru'><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{heading}</title><style>body{{font:16px/1.5 system-ui;max-width:850px;"
            "margin:2rem auto;padding:0 1rem}article{border-bottom:1px solid #ddd;padding:.6rem 0}"
            "p{white-space:pre-wrap;margin:.3rem 0}</style>"
            f"<h1>{heading}</h1><p>{escape(record['language'])} · {escape(record['channel'])}</p>"
            + "".join(rows) + "</html>")


@app.post("/api/conversations/sync")
def sync_conversations() -> dict:
    if not DEV_PATH.exists() and not os.environ.get("DATA_API_URL", "").strip():
        raise HTTPException(status_code=503, detail="Источник DEV-диалогов не настроен")
    try:
        count = import_conversations()
    except (HTTPError, URLError, TimeoutError, ValueError, OSError):
        raise HTTPException(status_code=502, detail="Не удалось загрузить диалоги из DEV API") from None
    return {"imported": count}


from .evaluations import router as evaluations_router  # noqa: E402

app.include_router(evaluations_router)
app.mount("/", StaticFiles(directory=ROOT / "web", html=True), name="web")
