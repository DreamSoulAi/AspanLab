"""
test_partner_e2e.py — РЕАЛЬНЫЙ end-to-end прогон роутинга partner_stt.

Гоняет настоящую цепочку: аудио → KK+RU параллельно (сеть) → детект галлюцинаций
→ выбор → итоговый текст. Печатает оба кандидата, вердикты слоя 1 и финальный engine.

Запуск (внутри контейнера, с партнёрскими URL в env):
    python test_partner_e2e.py /app/uploads/test.wav /app/uploads/test2.wav /app/uploads/test3.wav

Ожидаем:
    test.wav  (казахский) → partner_kk
    test2.wav (русский)   → partner_ru
    test3.wav (смесь)     → partner_kk

Фолбэк-кейс (партнёр выключен) — запусти с битыми URL:
    PARTNER_KK_URL=http://2.133.48.5:9999/v1 PARTNER_RU_URL=http://2.133.48.5:9999/v1 \
        python test_partner_e2e.py /app/uploads/test.wav
    Ожидаем: engine=partner_both_failed, text=None → audio_analyzer уйдёт на gpt-4o.
"""

import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Включаем INFO чтобы видеть вердикты слоя 1 ([kk]/[ru]) и выбор
logging.basicConfig(level=logging.INFO, format="    %(message)s")

from backend.services import partner_stt


async def run_one(path: str):
    name = os.path.basename(path)
    print("=" * 70)
    print(f"  ФАЙЛ: {name}")
    print(f"  PARTNER_KK_URL = {partner_stt.PARTNER_KK_URL}")
    print(f"  PARTNER_RU_URL = {partner_stt.PARTNER_RU_URL}")
    print("-" * 70)
    if not os.path.exists(path):
        print(f"  ⚠ файл не найден: {path}")
        return
    wav = open(path, "rb").read()
    text, engine = await partner_stt.transcribe(wav)
    print("-" * 70)
    print(f"  ИТОГ engine = {engine}")
    if text is None:
        print(f"  ИТОГ text   = None  →  audio_analyzer уйдёт на gpt-4o фолбэк ✓")
    else:
        print(f"  ИТОГ слов   = {len(text.split())}")
        print(f"  ИТОГ текст  = {text[:300]}")
    print()


async def main(paths):
    if not partner_stt.is_enabled():
        print("⚠ partner_stt НЕ включён — задай PARTNER_KK_URL и PARTNER_RU_URL в env")
        return 1
    for p in paths:
        await run_one(p)
    return 0


if __name__ == "__main__":
    paths = sys.argv[1:]
    if not paths:
        print("Укажи пути к WAV-файлам")
        sys.exit(1)
    sys.exit(asyncio.run(main(paths)) or 0)
