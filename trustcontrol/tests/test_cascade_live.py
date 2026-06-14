#!/usr/bin/env python3
"""
Прогоняет WAV-файл через полный каскад TrustControl и показывает каждый шаг.

Запуск из корня проекта:
  python tests/test_cascade_live.py audio.wav
  python tests/test_cascade_live.py audio.wav --context "кофейня"
  python tests/test_cascade_live.py recordings/  # все WAV в папке

Нужны переменные окружения (можно в .env):
  OPENAI_API_KEY=...
  ISSAI_WORKER_URL=http://...       # казахский гейт
  RUSSIAN_WORKER_URL=http://...     # русский гейт (опционально)
  RUSSIAN_WORKER_KEY=ru_secret_key  # если задан ключ
"""

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

# Подгружаем .env если есть
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# Добавляем корень проекта в путь
sys.path.insert(0, str(Path(__file__).parent.parent))


async def run_one(wav_path: str, context: str) -> dict:
    from backend.services.audio_analyzer import analyze_audio_with_fallback

    with open(wav_path, "rb") as f:
        wav_bytes = f.read()

    t0 = time.time()
    result = await analyze_audio_with_fallback(
        wav_bytes=wav_bytes,
        transcript_text=None,
        business_context=context,
    )
    elapsed = time.time() - t0
    result["_elapsed"] = round(elapsed, 2)
    return result


def print_result(path: str, res: dict):
    status      = res.get("status", "?")
    text        = res.get("text") or res.get("transcript") or ""
    engine      = (res.get("_stt_diag") or {}).get("engine", "?")
    elapsed     = res.get("_elapsed", "?")
    is_biz      = res.get("is_business")
    fraud       = res.get("fraud_attempt")
    fraud_conf  = res.get("fraud_confidence")
    alerts      = res.get("alerts") or []

    sep = "─" * 60
    print(f"\n{sep}")
    print(f"Файл:    {path}")
    print(f"Статус:  {status}  |  Движок: {engine}  |  Время: {elapsed}с")

    if text:
        preview = text[:200] + ("…" if len(text) > 200 else "")
        print(f"Текст:   {preview!r}")

    if is_biz is not None:
        print(f"Бизнес:  {'да' if is_biz else 'нет'}")

    if fraud is not None:
        print(f"Фрод:    {'⚠️  ДА' if fraud else 'нет'}  (уверенность {fraud_conf}%)")

    if alerts:
        print(f"Тревоги: {', '.join(alerts)}")

    # Детали каскада
    diag = res.get("_stt_diag") or {}
    if diag:
        print(f"Диагностика: {json.dumps(diag, ensure_ascii=False, indent=2)}")

    print(sep)


async def main():
    parser = argparse.ArgumentParser(description="Тест каскада на реальных записях")
    parser.add_argument("paths", nargs="+", help="WAV файл(ы) или папка с WAV")
    parser.add_argument("--context", default="кофейня", help="Бизнес-контекст (default: кофейня)")
    args = parser.parse_args()

    # Собираем все WAV файлы
    wav_files = []
    for p in args.paths:
        path = Path(p)
        if path.is_dir():
            wav_files.extend(sorted(path.glob("*.wav")))
        elif path.suffix.lower() == ".wav":
            wav_files.append(path)
        else:
            print(f"⚠️  Пропускаю (не WAV): {p}")

    if not wav_files:
        print("Нет WAV файлов для обработки")
        sys.exit(1)

    print(f"Файлов: {len(wav_files)}  |  Контекст: {args.context}")

    stats = {"IGNORE": 0, "PERSONAL": 0, "ok": 0, "ERROR": 0}

    for wav in wav_files:
        try:
            res = await run_one(str(wav), args.context)
            print_result(str(wav), res)
            s = res.get("status", "ok")
            key = s if s in stats else "ok"
            stats[key] = stats.get(key, 0) + 1
        except Exception as e:
            print(f"\n❌ {wav}: {type(e).__name__}: {e}")
            stats["ERROR"] += 1

    if len(wav_files) > 1:
        print(f"\nИтого: {dict(stats)}")


if __name__ == "__main__":
    asyncio.run(main())
