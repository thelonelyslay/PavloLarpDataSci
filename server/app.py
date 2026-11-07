"""FastAPI entry point and persistent DEV conversation import."""
from __future__ import annotations

import json
import os
import sqlite3
from uuid import uuid4
from html import escape
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse

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
        with urlopen(request, timeout=20) as response:
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
        raise HTTPException(status_code=404, detail="Checklist not found")
    return _checklist_response(row, include_json=True)


@app.post("/api/checklists", status_code=201)
def create_checklist(checklist: dict) -> dict:
    """Save an immutable version; activate it with the separate activate endpoint."""
    channel = checklist.get("applies_to")
    name = checklist.get("name")
    if channel not in ("agent chat", "voice call") or not isinstance(name, str) or not name.strip():
        raise HTTPException(status_code=422, detail="name and valid applies_to are required")
    try:
        validate_checklist(checklist)
    except (ValueError, KeyError, TypeError) as exc:
        raise HTTPException(status_code=422, detail=f"Invalid checklist: {exc}") from None
    new_id = uuid4().hex
    with _connect() as db:
        db.execute("BEGIN IMMEDIATE")
        base_id = checklist.get("id") or checklist.get("base_id")
        if base_id:
            base = db.execute("SELECT name, applies_to FROM checklists WHERE id = ?", (base_id,)).fetchone()
            if base is None:
                raise HTTPException(status_code=404, detail="Base checklist not found")
            if base["name"] != name or base["applies_to"] != channel:
                raise HTTPException(status_code=422, detail="Base checklist name or channel differs")
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
            raise HTTPException(status_code=404, detail="Checklist not found")
        db.execute("UPDATE checklists SET active = 0 WHERE applies_to = ?", (row["applies_to"],))
        db.execute("UPDATE checklists SET active = 1 WHERE id = ?", (checklist_id,))
        row = db.execute("SELECT * FROM checklists WHERE id = ?", (checklist_id,)).fetchone()
    return _checklist_response(row)


@app.get("/api/conversations")
def list_conversations() -> list[dict]:
    with _connect() as db:
        rows = db.execute("""SELECT c.id, c.channel, c.language,
            (SELECT e.total FROM evaluations e WHERE e.conversation_id = c.id
             ORDER BY e.created_at DESC, e.rowid DESC LIMIT 1) AS total
            FROM conversations c ORDER BY c.id""").fetchall()
    return [{"id": row["id"], "channel": row["channel"], "language": row["language"],
             "total": row["total"]} for row in rows]


@app.get("/api/conversations/{conversation_id}")
def get_conversation(conversation_id: str) -> dict:
    with _connect() as db:
        row = db.execute("SELECT * FROM conversations WHERE id = ?", (conversation_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
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
        raise HTTPException(status_code=503, detail="DEV data source is not configured")
    try:
        count = import_conversations()
    except (HTTPError, URLError, TimeoutError, ValueError, OSError):
        raise HTTPException(status_code=502, detail="Data API import failed") from None
    return {"imported": count}


from .evaluations import router as evaluations_router  # noqa: E402

app.include_router(evaluations_router)
