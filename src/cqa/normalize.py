"""Normalize incoming dialog records: sources spell language, channel and speaker codes differently.

Used by the server loader, export_results.py and validate_results.py. Unknown codes are kept as they are
(no guessing). Message order and count never change, so message_index / evidence turns stay valid.
"""
from __future__ import annotations

LANGUAGES = {"ru": "RU", "kz": "KZ", "kk": "KZ", "kaz": "KZ", "en": "EN", "eng": "EN"}
CHANNELS = {"agent chat": "agent chat", "agent_chat": "agent chat", "chat": "agent chat",
            "voice call": "voice call", "voice_call": "voice call", "call": "voice call", "voice": "voice call"}
SPEAKERS = {"operator": "operator", "agent": "operator", "manager": "operator",
            "client": "client", "customer": "client", "bot": "bot", "system": "system"}


def normalize_record(record: dict) -> tuple[dict, list[str]]:
    """Copy of the record with canonical codes (RU/KZ/EN, agent chat/voice call, operator/client/bot/system)
    and warnings, e.g. when no operator message is left after normalization."""
    rec = dict(record)
    lang = str(rec.get("language") or "").strip()
    rec["language"] = LANGUAGES.get(lang.lower(), lang.upper())
    channel = str(rec.get("channel") or "").strip()
    rec["channel"] = CHANNELS.get(channel.lower(), channel)
    transcript = rec.get("transcript")
    if isinstance(transcript, list):
        rec["transcript"] = [
            {**m, "speaker": SPEAKERS.get(str(m.get("speaker", "")).strip().lower(), m.get("speaker"))}
            if isinstance(m, dict) else m for m in transcript]
    warnings = []
    cid = rec.get("conversation_id") or rec.get("id") or "?"
    if isinstance(rec.get("transcript"), list) and not any(
            isinstance(m, dict) and m.get("speaker") == "operator" for m in rec["transcript"]):
        warnings.append(f"диалог {cid}: нет ни одной реплики оператора (роль operator) — "
                        "оценка работы оператора будет неточной")
    return rec, warnings
