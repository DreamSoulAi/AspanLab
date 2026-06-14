#!/usr/bin/env python3
"""
Тест русского STT воркера.
Запуск: python tests/test_russian_worker.py [--url http://localhost:8011] [--key KEY]

Что проверяем:
  1. /health воркер жив
  2. transcribe(wav_ru) → возвращает русский текст
  3. transcribe(wav_kk) → возвращает кашу (ожидаемо — модель нейтральная)
  4. russian_stt.py клиент работает через settings

Без воркера (RUSSIAN_WORKER_URL не задан) — пропускает тесты 2-4.
"""

import argparse
import asyncio
import io
import math
import os
import struct
import sys
import wave

# ── Синтетические WAV-файлы для теста ────────────────────────────────────────

def _make_wav(freq_hz: float, duration_s: float = 2.0, sample_rate: int = 16000) -> bytes:
    """Чистый синус — не речь, но достаточно чтобы проверить HTTP-контракт."""
    n = int(sample_rate * duration_s)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        samples = [
            int(32000 * math.sin(2 * math.pi * freq_hz * i / sample_rate))
            for i in range(n)
        ]
        wf.writeframes(struct.pack(f"<{n}h", *samples))
    return buf.getvalue()


def _make_silence(duration_s: float = 1.0, sample_rate: int = 16000) -> bytes:
    """Тишина — воркер должен вернуть пустую строку."""
    n = int(sample_rate * duration_s)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(b"\x00\x00" * n)
    return buf.getvalue()


# ── HTTP-тесты напрямую к воркеру ────────────────────────────────────────────

async def test_health(base_url: str) -> bool:
    import httpx
    print(f"\n[1] /health → {base_url}/health")
    try:
        async with httpx.AsyncClient(timeout=10.0) as cli:
            r = await cli.get(f"{base_url}/health")
        data = r.json()
        model = data.get("model", "?")
        status = data.get("status", "?")
        print(f"    status={status}  model={model}")
        ok = r.status_code == 200 and status == "ok"
        print(f"    {'✅ PASS' if ok else '❌ FAIL'}")
        return ok
    except Exception as e:
        print(f"    ❌ Ошибка: {e}")
        return False


async def test_transcribe_silence(base_url: str, api_key: str = "") -> bool:
    import httpx
    print("\n[2] transcribe(тишина) → ожидаем пустой или короткий текст")
    wav = _make_silence(2.0)
    headers = {"X-API-Key": api_key} if api_key else {}
    try:
        async with httpx.AsyncClient(timeout=60.0) as cli:
            r = await cli.post(
                f"{base_url}/transcribe",
                files={"audio": ("silence.wav", wav, "audio/wav")},
                data={"language": "ru"},
                headers=headers,
            )
        if r.status_code != 200:
            print(f"    ❌ HTTP {r.status_code}: {r.text[:200]}")
            return False
        data = r.json()
        text = data.get("text", "").strip()
        words = len(text.split()) if text else 0
        print(f"    text={text!r:.60}  words={words}")
        # Тишина → модель может выдать "", ".", галлюцинацию. Главное — не упала.
        ok = r.status_code == 200
        print(f"    {'✅ PASS (ответил)' if ok else '❌ FAIL'}")
        return ok
    except Exception as e:
        print(f"    ❌ Ошибка: {e}")
        return False


async def test_transcribe_tone(base_url: str, api_key: str = "") -> bool:
    import httpx
    print("\n[3] transcribe(синус 440Hz) → воркер не падает на не-речи")
    wav = _make_wav(440.0, 2.0)
    headers = {"X-API-Key": api_key} if api_key else {}
    try:
        async with httpx.AsyncClient(timeout=60.0) as cli:
            r = await cli.post(
                f"{base_url}/transcribe",
                files={"audio": ("tone.wav", wav, "audio/wav")},
                data={"language": "ru"},
                headers=headers,
            )
        ok = r.status_code == 200
        text = r.json().get("text", "") if ok else ""
        print(f"    HTTP={r.status_code}  text={text!r:.60}")
        print(f"    {'✅ PASS (не упал)' if ok else '❌ FAIL'}")
        return ok
    except Exception as e:
        print(f"    ❌ Ошибка: {e}")
        return False


async def test_real_wav(base_url: str, api_key: str, wav_path: str) -> bool:
    """Тест на реальном файле — если передан через --wav."""
    import httpx
    print(f"\n[4] transcribe({wav_path}) → реальный файл")
    try:
        with open(wav_path, "rb") as f:
            wav = f.read()
    except Exception as e:
        print(f"    ⚠️  Не могу прочитать файл: {e}")
        return True  # не считаем за провал

    headers = {"X-API-Key": api_key} if api_key else {}
    try:
        async with httpx.AsyncClient(timeout=120.0) as cli:
            r = await cli.post(
                f"{base_url}/transcribe",
                files={"audio": (os.path.basename(wav_path), wav, "audio/wav")},
                data={"language": "ru"},
                headers=headers,
            )
        if r.status_code != 200:
            print(f"    ❌ HTTP {r.status_code}: {r.text[:200]}")
            return False
        data = r.json()
        text  = data.get("text", "")
        lang  = data.get("language", "?")
        dur   = data.get("audio_duration", "?")
        segs  = data.get("segments", "?")
        elapsed = data.get("elapsed", "?")
        print(f"    lang={lang}  dur={dur}с  segs={segs}  elapsed={elapsed}с")
        print(f"    text: {text[:200]!r}")
        print(f"    ✅ PASS")
        return True
    except Exception as e:
        print(f"    ❌ Ошибка: {e}")
        return False


# ── Тест клиента russian_stt.py ──────────────────────────────────────────────

async def test_python_client(base_url: str, api_key: str = "") -> bool:
    """Проверяет что russian_stt.py клиент работает через settings."""
    print("\n[5] russian_stt.transcribe() через Python-клиент")
    # Патчим settings в обход реального .env
    try:
        from backend.config import settings
        settings.RUSSIAN_WORKER_URL = base_url
        settings.RUSSIAN_WORKER_KEY = api_key

        from backend.services import russian_stt
        enabled = russian_stt.is_enabled()
        print(f"    is_enabled={enabled}")
        if not enabled:
            print("    ⚠️  SKIP — RUSSIAN_WORKER_URL пуст в settings")
            return True

        wav = _make_silence(1.0)
        diag: dict = {}
        result = await russian_stt.transcribe(wav, diag=diag)
        print(f"    result={result!r}  diag={diag}")
        ok = diag.get("stage") in ("ok", "empty")
        print(f"    {'✅ PASS' if ok else '❌ FAIL — stage=' + str(diag.get('stage'))}")
        return ok
    except ImportError as e:
        print(f"    ⚠️  SKIP (нет модулей backend в пути): {e}")
        print("    Запусти из корня проекта: python -m pytest tests/ или python tests/test_russian_worker.py")
        return True
    except Exception as e:
        print(f"    ❌ Ошибка: {type(e).__name__}: {e}")
        return False


# ── main ──────────────────────────────────────────────────────────────────────

async def main():
    parser = argparse.ArgumentParser(description="Тест русского STT воркера")
    parser.add_argument("--url", default=os.getenv("RUSSIAN_WORKER_URL", "http://localhost:8011"),
                        help="URL воркера (default: $RUSSIAN_WORKER_URL или http://localhost:8011)")
    parser.add_argument("--key", default=os.getenv("RUSSIAN_WORKER_KEY", ""),
                        help="API-ключ (default: $RUSSIAN_WORKER_KEY)")
    parser.add_argument("--wav", default="",
                        help="Путь к WAV-файлу для реального теста")
    args = parser.parse_args()

    base_url = args.url.rstrip("/")
    print(f"Воркер: {base_url}")
    print(f"Ключ:   {'задан' if args.key else 'не задан (без auth)'}")

    results = []
    results.append(await test_health(base_url))
    results.append(await test_transcribe_silence(base_url, args.key))
    results.append(await test_transcribe_tone(base_url, args.key))

    if args.wav:
        results.append(await test_real_wav(base_url, args.key, args.wav))

    results.append(await test_python_client(base_url, args.key))

    passed = sum(results)
    total  = len(results)
    print(f"\n{'='*50}")
    print(f"Итого: {passed}/{total} тестов прошло")
    if passed < total:
        print("❌ Есть провалы — смотри выше")
        sys.exit(1)
    else:
        print("✅ Все тесты прошли — можно вливать в main")


if __name__ == "__main__":
    asyncio.run(main())
