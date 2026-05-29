#!/usr/bin/env python3
# ════════════════════════════════════════════════════════════
#  ISSAI STT Воркер — inference-сервер для TrustControl
#  Модель: abilmansplus/whisper-turbo-ksc2 (Hugging Face)
#
#  Запуск:
#    pip install -r requirements-issai.txt
#    python issai_worker.py
#
#  Docker:
#    docker build -f Dockerfile.issai -t issai-worker .
#    docker run -p 8010:8010 --env-file .env issai-worker
#
#  Переменные окружения:
#    ISSAI_MODEL   — путь или HF repo (default: abilmansplus/whisper-turbo-ksc2)
#    ISSAI_DEVICE  — cpu | cuda (default: cpu)
#    ISSAI_COMPUTE — int8 | float16 | float32 (default: int8 для CPU, float16 для GPU)
#    ISSAI_PORT    — порт (default: 8010)
#    ISSAI_WORKERS — кол-во uvicorn воркеров (default: 1)
#    ISSAI_API_KEY — если задан, требует X-API-Key заголовок
#
#  Масштабирование:
#    2-3 кассы → 1 CPU-воркер (4 ядра, 4GB RAM)
#    10-20 касс → 2 CPU-воркера за nginx (или 1 GPU)
#    50-100 касс → GPU (RTX 3060/3080) + 2-3 CPU резерв
#
#  Очередь:
#    asyncio.Lock() — каждый запрос ждёт своей очереди.
#    Для CPU whisper-large-v3-turbo: ~5-10с/запрос.
#    При 100 точках пиковая нагрузка ~3-5 одновременных запросов.
# ════════════════════════════════════════════════════════════

import asyncio
import io
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Optional

import uvicorn
from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile

log = logging.getLogger("issai_worker")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

# ── Конфигурация ─────────────────────────────────────────────
MODEL_ID      = os.getenv("ISSAI_MODEL",   "abilmansplus/whisper-turbo-ksc2")
DEVICE        = os.getenv("ISSAI_DEVICE",  "cpu")
COMPUTE_TYPE  = os.getenv("ISSAI_COMPUTE", "int8" if DEVICE == "cpu" else "float16")
PORT          = int(os.getenv("ISSAI_PORT",    8010))
NUM_WORKERS   = int(os.getenv("ISSAI_WORKERS", 1))
API_KEY       = os.getenv("ISSAI_API_KEY", "")

# Глобальные объекты модели
_model        = None
_model_lock   = asyncio.Lock()    # очередь: один запрос за раз (CPU-режим)


def _load_model():
    """Загружает модель при старте. Вызывается один раз."""
    global _model
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        raise RuntimeError(
            "faster-whisper не установлен. "
            "Запусти: pip install -r requirements-issai.txt"
        )

    log.info(f"Загрузка модели {MODEL_ID} (device={DEVICE}, compute={COMPUTE_TYPE})...")
    t0 = time.time()
    _model = WhisperModel(
        MODEL_ID,
        device=DEVICE,
        compute_type=COMPUTE_TYPE,
        download_root=os.getenv("ISSAI_CACHE_DIR", None),
        # CPU: параллельно по 4 потока на модель (не мешает Lock)
        cpu_threads=int(os.getenv("ISSAI_THREADS", 4)),
        num_workers=1,
    )
    log.info(f"Модель загружена за {time.time()-t0:.1f}с")


@asynccontextmanager
async def lifespan(app: FastAPI):
    _load_model()
    yield
    log.info("Воркер остановлен")


app = FastAPI(title="ISSAI STT Worker", version="1.0", lifespan=lifespan)


# ── Авторизация ──────────────────────────────────────────────
def _check_auth(x_api_key: Optional[str]):
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")


# ── Эндпоинты ────────────────────────────────────────────────

@app.get("/health")
async def health(x_api_key: Optional[str] = Header(default=None)):
    _check_auth(x_api_key)
    return {
        "status":  "ok",
        "model":   MODEL_ID,
        "device":  DEVICE,
        "compute": COMPUTE_TYPE,
    }


@app.post("/transcribe")
async def transcribe(
    audio:     UploadFile = File(...),
    language:  str        = Form(default="kk"),
    x_api_key: Optional[str] = Header(default=None),
):
    """
    Принимает аудио-файл (любой формат поддерживаемый ffmpeg),
    возвращает транскрипцию.

    Body (multipart/form-data):
      audio    — файл (wav/mp3/ogg/webm/…)
      language — ISO-639-1 код: kk | ru | en (default: kk)

    Response:
      {"text": "...", "language": "kk", "duration": 12.3, "segments": 3}
    """
    _check_auth(x_api_key)

    if _model is None:
        raise HTTPException(status_code=503, detail="Модель не загружена")

    audio_bytes = await audio.read()
    if not audio_bytes:
        raise HTTPException(status_code=400, detail="Пустой файл")

    # Нормализуем код языка
    lang = (language or "kk").split("-")[0].lower()
    # whisper-turbo-ksc2 лучше всего работает с kk; для шала-казахского тоже kk
    # Для явно русских файлов → ru; для остальных → None (auto-detect)
    whisper_lang = lang if lang in ("kk", "ru", "en") else None

    t0 = time.time()
    log.info(f"Запрос: {len(audio_bytes)/1024:.0f}KB | lang={whisper_lang or 'auto'}")

    # Одновременно работает только один запрос (CPU ограничение)
    async with _model_lock:
        result_text, result_lang, total_dur, num_segs = await asyncio.get_event_loop().run_in_executor(
            None, _run_inference, audio_bytes, whisper_lang
        )

    elapsed = time.time() - t0
    log.info(
        f"Готово: {len(result_text)} симв | dur={total_dur:.1f}с "
        f"| segs={num_segs} | elapsed={elapsed:.1f}с | {result_text[:80]!r}"
    )

    return {
        "text":     result_text,
        "language": result_lang,
        "duration": round(total_dur, 2),
        "segments": num_segs,
        "elapsed":  round(elapsed, 2),
    }


def _run_inference(audio_bytes: bytes, language: Optional[str]) -> tuple:
    """
    Синхронная инференция. Запускается в executor, не блокирует event loop.
    Возвращает (text, language, total_duration, num_segments).
    """
    audio_buf = io.BytesIO(audio_bytes)

    segments, info = _model.transcribe(
        audio_buf,
        language=language,
        task="transcribe",
        beam_size=5,
        best_of=5,
        # Контекстная подсказка для шала-казахского:
        # модель лучше угадывает казахско-русские слова
        initial_prompt=(
            "Запись разговора с кассы в Казахстане. "
            "Шала-казахский: смесь казахского и русского. "
            "Сәлем, рахмет, теңге, картамен, жоқ, ия, тез."
        ),
        # Подавить галлюцинации на тишине
        no_speech_threshold=0.6,
        # Логарифмическая вероятность — если очень низкая, это шум
        log_prob_threshold=-1.0,
        # Не сжимать повторяющиеся токены (артефакты на шуме)
        compression_ratio_threshold=2.4,
        vad_filter=True,               # VAD убирает тишину до и после речи
        vad_parameters=dict(
            min_silence_duration_ms=500,
            speech_pad_ms=200,
        ),
    )

    parts = []
    total_dur = 0.0
    num_segs = 0
    for seg in segments:
        text = (seg.text or "").strip()
        if text:
            parts.append(text)
        total_dur = seg.end
        num_segs += 1

    result_text = " ".join(parts).strip()
    result_lang = info.language or (language or "kk")
    return result_text, result_lang, total_dur, num_segs


if __name__ == "__main__":
    uvicorn.run(
        "issai_worker:app",
        host="0.0.0.0",
        port=PORT,
        workers=NUM_WORKERS,
        log_level="info",
    )
