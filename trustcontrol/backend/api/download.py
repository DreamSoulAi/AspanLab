import asyncio
import logging
import os
import re
import shutil
import tempfile
import time
import zipfile
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

_log = logging.getLogger("download")

from backend.api.auth import get_current_user
from backend.models.user import User
from backend.database import get_db

router = APIRouter()

# ── Кэш .exe на ДИСКЕ (не в RAM!) ────────────────────────────────────────────
# Релизный zip ~105 МБ. Грузить его целиком в память нельзя — на free-tier
# Render (512 МБ) это вызывает OOM, функция падает и клиент видит 503.
# Поэтому: zip качаем потоком на диск, .exe извлекаем потоком в кэш-файл,
# выходной архив тоже собираем на диске и отдаём чанками. Пик памяти ~КБ.
_EXE_CACHE_PATH = Path(tempfile.gettempdir()) / "trustcontrol_monitor.exe"
_EXE_CACHE_TTL = 3600  # 1 час
_EXE_LOCK = asyncio.Lock()
_EXE_RELEASE_URL = (
    "https://github.com/dreamsoulai/aspanlab/releases/download/"
    "windows-latest/TrustControl_Windows.zip"
)
_CHUNK = 1 << 16  # 64 КБ


def _exe_is_fresh() -> bool:
    try:
        return (
            _EXE_CACHE_PATH.exists()
            and _EXE_CACHE_PATH.stat().st_size > 0
            and (time.time() - _EXE_CACHE_PATH.stat().st_mtime) < _EXE_CACHE_TTL
        )
    except OSError:
        return False


async def _ensure_exe_file() -> Path | None:
    """Гарантирует свежий TrustControl.exe в кэше на диске. Возвращает путь или None."""
    if _exe_is_fresh():
        return _EXE_CACHE_PATH

    async with _EXE_LOCK:
        # Мог скачать другой запрос пока ждали лок.
        if _exe_is_fresh():
            return _EXE_CACHE_PATH

        tmp_zip = None
        try:
            import httpx
            # 1) Качаем релизный zip ПОТОКОМ на диск (память не растёт).
            fd, tmp_zip = tempfile.mkstemp(suffix=".zip", prefix="tc_rel_")
            with os.fdopen(fd, "wb") as out:
                async with httpx.AsyncClient(follow_redirects=True, timeout=300) as c:
                    async with c.stream("GET", _EXE_RELEASE_URL) as resp:
                        resp.raise_for_status()
                        async for chunk in resp.aiter_bytes(_CHUNK):
                            out.write(chunk)

            # 2) Извлекаем .exe ПОТОКОМ в кэш-файл.
            with zipfile.ZipFile(tmp_zip) as zf:
                exe_name = next(
                    (n for n in zf.namelist() if n.lower().endswith(".exe")), None
                )
                if not exe_name:
                    _log.warning("В архиве релиза не найден .exe")
                    return None
                tmp_exe = _EXE_CACHE_PATH.with_suffix(".exe.part")
                with zf.open(exe_name) as src, open(tmp_exe, "wb") as dst:
                    shutil.copyfileobj(src, dst, _CHUNK)
                os.replace(tmp_exe, _EXE_CACHE_PATH)  # атомарно

            _log.info(f"Кэширован .exe {_EXE_CACHE_PATH.stat().st_size // 1024} KB из релиза")
            return _EXE_CACHE_PATH
        except Exception as e:
            _log.warning(f"Не удалось получить .exe из релиза: {e}")
            return None
        finally:
            if tmp_zip and os.path.exists(tmp_zip):
                try:
                    os.remove(tmp_zip)
                except OSError:
                    pass


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


def _build_zip_file(exe_path: Path, config: str, readme: str, folder: str) -> str:
    """Собирает выходной zip на диске (.exe не recompress) и возвращает путь к нему."""
    fd, out_zip = tempfile.mkstemp(suffix=".zip", prefix="tc_out_")
    os.close(fd)
    try:
        with zipfile.ZipFile(out_zip, "w") as zf:
            # .exe уже сжат PyInstaller → ZIP_STORED (без CPU на повторное сжатие).
            zf.write(exe_path, f"{folder}/TrustControl.exe", compress_type=zipfile.ZIP_STORED)
            zf.writestr(f"{folder}/config.ini", config, compress_type=zipfile.ZIP_DEFLATED)
            zf.writestr(f"{folder}/README.txt", readme, compress_type=zipfile.ZIP_DEFLATED)
        return out_zip
    except Exception:
        if os.path.exists(out_zip):
            os.remove(out_zip)
        raise


def _stream_and_cleanup(path: str):
    """Отдаёт файл чанками и удаляет его после завершения."""
    try:
        with open(path, "rb") as f:
            while True:
                chunk = f.read(_CHUNK)
                if not chunk:
                    break
                yield chunk
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


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

        exe_path = await _ensure_exe_file()
        if not exe_path:
            # .exe временно недоступен (релиз ещё собирается или GitHub не отвечает).
            # Раньше тут был Python-фолбэк — убран: клиент НИКОГДА не должен видеть Python.
            raise HTTPException(
                status_code=503,
                detail="Программа сейчас обновляется. Подождите минуту и попробуйте снова.",
            )

        out_zip = _build_zip_file(exe_path, config, _readme_exe(loc.name or ""), f"TrustControl_{safe}")
        _log.info(f"Выдан .exe-архив для точки {location_id} ({safe})")

        return StreamingResponse(
            _stream_and_cleanup(out_zip),
            media_type="application/zip",
            headers={"Content-Disposition": f"attachment; filename=TrustControl_{safe}.zip"},
        )
    except HTTPException:
        raise
    except Exception as exc:
        _log.exception(f"download_installer_for_location failed loc={location_id}: {exc}")
        raise HTTPException(status_code=500, detail=f"Ошибка сборки архива: {exc}")
