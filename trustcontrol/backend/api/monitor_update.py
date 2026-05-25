# ════════════════════════════════════════════════════════════
#  API: Автообновление монитора
#  GET /api/monitor/version  — текущий MD5 монитора
#  GET /api/monitor/download — скачать актуальный monitor.py
# ════════════════════════════════════════════════════════════

import hashlib
from pathlib import Path

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse

from backend.api.deps import get_location_by_api_key
from backend.models.location import Location

router = APIRouter()

_MONITOR_PATH = Path(__file__).parent.parent / "worker" / "monitor.py"


def _monitor_hash() -> str:
    return hashlib.md5(_MONITOR_PATH.read_bytes()).hexdigest()


@router.get("/version")
async def monitor_version(loc: Location = Depends(get_location_by_api_key)):
    """Возвращает MD5 актуального monitor.py. Монитор сравнивает со своим хэшем."""
    if not _MONITOR_PATH.exists():
        return {"version": "unknown"}
    return {"version": _monitor_hash()}


@router.get("/download")
async def monitor_download(loc: Location = Depends(get_location_by_api_key)):
    """Отдаёт актуальный monitor.py для самообновления."""
    if not _MONITOR_PATH.exists():
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="monitor.py не найден на сервере")
    return FileResponse(
        path=str(_MONITOR_PATH),
        filename="monitor.py",
        media_type="application/octet-stream",
    )
