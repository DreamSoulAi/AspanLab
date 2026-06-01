#!/usr/bin/env python3
# ════════════════════════════════════════════════════════════
#  Диагностика S3 + аудио-архива.
#
#  Запуск в Render Shell:
#    python scripts/check_s3.py
#
#  Проверяет:
#    1. Все ли env-переменные S3 выставлены
#    2. Реально ли грузится файл в бакет (тестовый WAV)
#    3. Открывается ли публичная ссылка (HTTP 200)
#    4. Сколько последних отчётов имеют s3_url в базе
#
#  Никаких диалогов с микрофоном — всё проверяется автоматически.
# ════════════════════════════════════════════════════════════

import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _line(label, value):
    print(f"  {label:<24} {value}")


async def main():
    from backend.config import settings

    print("\n=== 1. ENV-переменные S3 ===")
    _line("S3_BUCKET",         settings.S3_BUCKET or "❌ ПУСТО")
    _line("S3_ENDPOINT_URL",   settings.S3_ENDPOINT_URL or "(AWS по умолчанию)")
    _line("S3_REGION",         settings.S3_REGION or "(us-east-1)")
    _line("AWS_ACCESS_KEY_ID", (settings.AWS_ACCESS_KEY_ID[:6] + "…") if settings.AWS_ACCESS_KEY_ID else "❌ ПУСТО")
    _line("AWS_SECRET…",       "задан ✓" if settings.AWS_SECRET_ACCESS_KEY else "❌ ПУСТО")

    if not settings.S3_BUCKET or not settings.AWS_ACCESS_KEY_ID or not settings.AWS_SECRET_ACCESS_KEY:
        print("\n❌ Не хватает ключей — S3 не заработает. Проверь Render → Environment.")
        return

    print("\n=== 2. Тестовая загрузка в S3 ===")
    # Минимальный валидный WAV (44 байта заголовок + тишина)
    import struct
    sample_rate = 16000
    n_samples = sample_rate  # 1 секунда тишины
    data = b"\x00\x00" * n_samples
    wav = (
        b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVE"
        + b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, sample_rate, sample_rate * 2, 2, 16)
        + b"data" + struct.pack("<I", len(data)) + data
    )

    from backend.services.storage import upload_evidence
    result = await upload_evidence(wav, location_id=0, report_id=999999)
    url = result.get("s3_url")
    if not url:
        print("  ❌ Загрузка вернула s3_url=None — смотри ошибку в логах выше (SignatureDoesNotMatch / AccessDenied).")
        return
    _line("Загружено ✓", url)

    print("\n=== 3. Доступна ли ссылка публично ===")
    try:
        import httpx
        async with httpx.AsyncClient(timeout=15.0) as cli:
            r = await cli.get(url)
        if r.status_code == 200:
            _line("HTTP 200 ✓", f"{len(r.content)} байт — ссылка открывается, аудио будет играть в браузере")
        elif r.status_code == 403:
            _line("HTTP 403 ❌", "файл загружен, но бакет/объект НЕ публичный — браузер не откроет")
            print("       → Yandex Object Storage → бакет → Доступ → Публичный (чтение объектов)")
        else:
            _line(f"HTTP {r.status_code}", r.text[:120])
    except Exception as e:
        _line("Ошибка запроса", str(e)[:120])

    print("\n=== 4. Последние отчёты в базе (есть ли s3_url) ===")
    try:
        from sqlalchemy import select
        from backend.database import AsyncSessionLocal
        from backend.models.report import Report
        async with AsyncSessionLocal() as db:
            rows = (await db.execute(
                select(Report).order_by(Report.timestamp.desc()).limit(5)
            )).scalars().all()
        if not rows:
            print("  (отчётов пока нет)")
        for r in rows:
            mark = "🎵" if r.s3_url else "—"
            print(f"  {mark} #{r.id} {r.timestamp:%d.%m %H:%M}  s3_url={'есть' if r.s3_url else 'НЕТ'}")
    except Exception as e:
        print(f"  Не удалось прочитать базу: {e}")

    print("\nГотово.\n")


if __name__ == "__main__":
    asyncio.run(main())
