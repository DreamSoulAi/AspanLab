import io
import logging
import re
import time
import zipfile
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

_log = logging.getLogger("download")

from backend.api.auth import get_current_user
from backend.models.user import User
from backend.database import get_db

router = APIRouter()

# ── Кэш скачанного .exe (обновляется раз в час) ──────────────────────────────
_EXE_BYTES: bytes | None = None
_EXE_CACHED_AT: float = 0.0
_EXE_CACHE_TTL = 3600  # 1 час
_EXE_RELEASE_URL = (
    "https://github.com/dreamsoulai/aspanlab/releases/download/"
    "windows-latest/TrustControl_Windows.zip"
)


async def _get_exe_bytes() -> bytes | None:
    """Скачивает TrustControl.exe из GitHub Release и кэширует в памяти."""
    global _EXE_BYTES, _EXE_CACHED_AT
    if _EXE_BYTES and (time.time() - _EXE_CACHED_AT) < _EXE_CACHE_TTL:
        return _EXE_BYTES
    try:
        import httpx
        async with httpx.AsyncClient(follow_redirects=True, timeout=120) as c:
            resp = await c.get(_EXE_RELEASE_URL)
            resp.raise_for_status()
        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            for name in zf.namelist():
                if name.lower().endswith(".exe"):
                    _EXE_BYTES = zf.read(name)
                    _EXE_CACHED_AT = time.time()
                    _log.info(f"Кэширован .exe {len(_EXE_BYTES)//1024} KB из релиза")
                    return _EXE_BYTES
        _log.warning("В архиве релиза не найден .exe")
    except Exception as e:
        _log.warning(f"Не удалось скачать .exe из релиза: {e}")
    return None


def _config_ini(api_url: str, api_key: str) -> str:
    return f"""[settings]
; Адрес сервера TrustControl
API_URL={api_url}

; API-ключ этой кассы — уже настроен автоматически
API_KEY={api_key}

; Язык: ru = русский, kk = казахский, auto = автоопределение
LANGUAGE=auto

; Секунд тишины = конец разговора (2.5 по умолчанию)
SILENCE=2.5

; Чувствительность микрофона 0-3 (2 по умолчанию)
VAD_LEVEL=3
"""


def _readme_exe(location_name: str) -> str:
    return f"""╔══════════════════════════════════════════════════╗
  TrustControl — Касса: {location_name}
  Ключ уже вписан. Устанавливать ничего не нужно!
╚══════════════════════════════════════════════════╝

ШАГ 1.  Распакуйте этот архив в любую папку.

ШАГ 2.  Дважды кликните на  TrustControl.exe
        Мониторинг запущен. Окно не закрывайте.

──────────────────────────────────────────────────
АВТОЗАПУСК при включении ПК:
  Правой кнопкой на TrustControl.exe → Создать ярлык
  Скопируйте ярлык в папку автозапуска Windows:
  C:\\Users\\...\\AppData\\Roaming\\Microsoft\\Windows\\
                    Start Menu\\Programs\\Startup
──────────────────────────────────────────────────
ЕСЛИ ЧТО-ТО НЕ РАБОТАЕТ:
  1. Проверьте интернет (открывается ли браузер?)
  2. Убедитесь что микрофон подключён к компьютеру
  3. Личный кабинет: https://trustcontrol.kz
"""


@router.get("/installer/{location_id}")
async def download_installer_for_location(
    location_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Персональный архив с уже вписанным API-ключом точки.
    Отдаёт готовый TrustControl.exe из GitHub Release — устанавливать
    Python или что-либо ещё НЕ нужно: распаковал → двойной клик → работает.
    """
    try:
        from backend.models.location import Location
        result = await db.execute(
            select(Location).where(Location.id == location_id, Location.owner_id == user.id)
        )
        loc = result.scalar()
        if not loc:
            raise HTTPException(status_code=404, detail="Точка не найдена")

        api_url = str(request.base_url).rstrip("/")
        safe = re.sub(r"[^\w\-]", "_", loc.name or "location", flags=re.ASCII).strip("_")[:30] or "location"
        config = _config_ini(api_url, loc.api_key or "")

        exe_bytes = await _get_exe_bytes()
        if not exe_bytes:
            # .exe временно недоступен (релиз ещё собирается или GitHub не отвечает).
            # Раньше тут был Python-фолбэк — убран: клиент НИКОГДА не должен видеть Python.
            raise HTTPException(
                status_code=503,
                detail="Программа сейчас обновляется. Подождите минуту и попробуйте снова.",
            )

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(f"TrustControl_{safe}/TrustControl.exe", exe_bytes)
            zf.writestr(f"TrustControl_{safe}/config.ini", config)
            zf.writestr(f"TrustControl_{safe}/README.txt", _readme_exe(loc.name or ""))
        buf.seek(0)
        _log.info(f"Выдан .exe-архив для точки {location_id} ({safe})")

        return StreamingResponse(
            buf,
            media_type="application/zip",
            headers={"Content-Disposition": f"attachment; filename=TrustControl_{safe}.zip"},
        )
    except HTTPException:
        raise
    except Exception as exc:
        _log.exception(f"download_installer_for_location failed loc={location_id}: {exc}")
        raise HTTPException(status_code=500, detail=f"Ошибка сборки архива: {exc}")
