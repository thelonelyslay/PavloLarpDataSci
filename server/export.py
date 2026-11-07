"""Download validated organizer-format results through the server."""
from __future__ import annotations

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


@router.get("/export")
def export_results(set: Literal["dev", "prod"], only_cached: bool = False) -> FileResponse:
    """Build, validate, then return results/<set>/results.json."""
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
            raise HTTPException(status_code=502,
                                detail="Выгрузка или проверка формата не прошла; проверьте настройки модели и данные")
        return FileResponse(destination, media_type="application/json", filename=f"{set}_results.json")
    finally:
        _export_lock.release()
