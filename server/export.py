"""Download validated organizer-format results through the server."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

ROOT = Path(__file__).resolve().parents[1]
router = APIRouter(prefix="/api")
_export_lock = threading.Lock()


def _log_tail(run: subprocess.CompletedProcess, lines: int = 10) -> str:
    """Last lines of the export script output (it prints no keys or data URLs)."""
    text = [l for l in (run.stdout + "\n" + run.stderr).splitlines() if l.strip() and "Warning" not in l]
    return "\n".join(text[-lines:]) or "(скрипт ничего не вывел)"


def _counts(path: Path) -> tuple[int, int]:
    results = json.loads(path.read_text(encoding="utf-8")).get("results", [])
    errors = sum(1 for r in results if r.get("status") == "error")
    return len(results) - errors, errors


@router.get("/export")
def export_results(set: Literal["dev", "prod"] = "dev", only_cached: bool = True) -> FileResponse:
    """Build, validate, then return results/<set>/results.json.

    Always by the standard repository checklists (export_results.py). By default only ready evaluations are
    used (no paid model calls); only_cached=false evaluates missing dialogs first.
    Headers X-Export-Ok / X-Export-Error give the number of ok and error records."""
    if not _export_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="Выгрузка уже выполняется")
    try:
        destination = ROOT / "results" / set / "results.json"
        command = [sys.executable, str(ROOT / "src" / "cqa" / "export_results.py"),
                   "--set", set, "--validate"]
        if only_cached:
            command.append("--only-cached")
        if set == "dev":
            if not (ROOT / "data" / "raw" / "dev.json").exists():
                if not os.environ.get("DATA_API_URL"):
                    raise HTTPException(status_code=503, detail="Источник DEV-диалогов не настроен")
                command.append("--from-api")
        else:
            prod_file = ROOT / "data" / "raw" / "prod.json"
            if prod_file.exists():
                command.extend(["--from-file", str(prod_file)])
            elif os.environ.get("PROD_GROUP_ID") and os.environ.get("DATA_API_URL"):
                command.extend(["--from-api", "--group-id", os.environ["PROD_GROUP_ID"]])
            else:
                raise HTTPException(status_code=503,
                                    detail="Для PROD нужен data/raw/prod.json или PROD_GROUP_ID в .env")
        try:
            run = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=3600, check=False)
        except subprocess.TimeoutExpired:
            raise HTTPException(status_code=504, detail="Превышено время ожидания выгрузки") from None
        if run.returncode != 0 or not destination.is_file():
            raise HTTPException(status_code=502, detail="Выгрузка или проверка формата не прошла. "
                                                        "Последние строки лога:\n" + _log_tail(run))
        ok, errors = _counts(destination)
        return FileResponse(destination, media_type="application/json", filename=f"{set}_results.json",
                            headers={"X-Export-Ok": str(ok), "X-Export-Error": str(errors),
                                     "Access-Control-Expose-Headers": "X-Export-Ok, X-Export-Error, Content-Disposition"})
    finally:
        _export_lock.release()
