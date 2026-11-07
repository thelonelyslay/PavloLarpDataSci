"""FastAPI entry point and persistent DEV conversation import."""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from fastapi import FastAPI, HTTPException

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


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def _init_db() -> None:
    with _connect() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS conversations (
            id TEXT PRIMARY KEY,
            channel TEXT NOT NULL,
            language TEXT NOT NULL,
            transcript TEXT NOT NULL,
            audio_url TEXT
        )""")


def import_conversations() -> int:
    """Fetch the DEV API server-side and upsert its records without logging content."""
    url = os.environ.get("DATA_API_URL", "").strip()
    if not url:
        return 0
    request = Request(url, headers={"Accept": "application/json", "User-Agent": "CQA-server/1.0"})
    with urlopen(request, timeout=20) as response:
        payload = json.load(response)
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
    if os.environ.get("DATA_API_URL", "").strip():
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


@app.get("/api/conversations")
def list_conversations() -> list[dict]:
    with _connect() as db:
        rows = db.execute("SELECT id, channel, language FROM conversations ORDER BY id").fetchall()
    return [{"id": row["id"], "channel": row["channel"], "language": row["language"],
             "total": None} for row in rows]


@app.get("/api/conversations/{conversation_id}")
def get_conversation(conversation_id: str) -> dict:
    with _connect() as db:
        row = db.execute("SELECT * FROM conversations WHERE id = ?", (conversation_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return {"id": row["id"], "channel": row["channel"], "language": row["language"],
            "transcript": json.loads(row["transcript"]), "audio_url": row["audio_url"]}


@app.post("/api/conversations/sync")
def sync_conversations() -> dict:
    if not os.environ.get("DATA_API_URL", "").strip():
        raise HTTPException(status_code=503, detail="DATA_API_URL is not configured")
    try:
        count = import_conversations()
    except (HTTPError, URLError, TimeoutError, ValueError, OSError):
        raise HTTPException(status_code=502, detail="Data API import failed") from None
    return {"imported": count}
