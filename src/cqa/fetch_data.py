"""Download DEV conversations from DATA_API_URL (.env) into data/raw/dev.json.

Usage: python3 src/cqa/fetch_data.py [--group-id ID] [--out data/raw/dev.json]
Prints only counts and one short example per language (never full dialogs, never the URL).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUT = ROOT / "data" / "raw" / "dev.json"


def with_group_id(url: str, group_id: str | None) -> str:
    if not group_id:
        return url
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != "group_id"]
    query.append(("group_id", group_id))
    return urlunsplit(parts._replace(query=urlencode(query)))


def fetch(url: str, retries: int = 2) -> dict:
    last_err: Exception | None = None
    for attempt in range(retries + 1):
        try:
            resp = requests.get(url, timeout=60)
            resp.raise_for_status()
            return resp.json()
        except (requests.RequestException, ValueError) as e:
            # print only the error type: the message may contain the URL
            last_err = e
            print(f"attempt {attempt + 1} failed: {type(e).__name__}", file=sys.stderr)
            time.sleep(2 * (attempt + 1))
    raise SystemExit(f"download failed after {retries + 1} attempts ({type(last_err).__name__})")


def short(text: str, n: int = 80) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 1] + "…"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--group-id", help="override group_id in DATA_API_URL")
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    args = ap.parse_args()

    url = dotenv_values(ROOT / ".env").get("DATA_API_URL")
    if not url:
        raise SystemExit("DATA_API_URL is empty in .env")

    payload = fetch(with_group_id(url, args.group_id))
    if not payload.get("isSuccess"):
        raise SystemExit("API returned isSuccess=false")
    records = (payload.get("data") or {}).get("records")
    if not isinstance(records, list):
        raise SystemExit("unexpected format: data.records is missing")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")

    langs = Counter(str(r.get("language")) for r in records)
    channels = Counter(str(r.get("channel")) for r in records)
    print(f"saved {len(records)} conversations -> {out}")
    print("languages:", ", ".join(f"{k}: {v}" for k, v in sorted(langs.items())))
    print("channels: ", ", ".join(f"{k}: {v}" for k, v in sorted(channels.items())))

    seen = set()
    for r in records:
        lang = r.get("language")
        if lang in seen:
            continue
        seen.add(lang)
        tr = r.get("transcript") or []
        first = next((m for m in tr if m.get("speaker") in ("client", "operator")), tr[0] if tr else {})
        print(f"[{lang}] id={r.get('conversation_id')} channel={r.get('channel')} "
              f"messages={len(tr)} | {first.get('speaker', '?')}: {short(first.get('text', ''))}")


if __name__ == "__main__":
    main()
