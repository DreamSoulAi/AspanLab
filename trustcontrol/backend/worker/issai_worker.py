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
import shutil
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

# Денойз стационарного шума (аппарат за кассой) перед распознаванием.
# Включён по умолчанию; выключить — ISSAI_DENOISE=false. prop=насколько давить.
DENOISE       = os.getenv("ISSAI_DENOISE", "true").lower() in ("1", "true", "yes", "on")
DENOISE_PROP  = float(os.getenv("ISSAI_DENOISE_PROP", 0.9))

# Куда складывать сконвертированную CT2-модель (риск №1)
CT2_CACHE_DIR = os.getenv("ISSAI_CT2_DIR", "/tmp/issai_ct2")
# Минимум RAM (МБ) для безопасного старта (риск №2)
MIN_RAM_MB    = int(os.getenv("ISSAI_MIN_RAM_MB", 3500))

# Глобальные объекты модели
_model        = None
_model_lock   = asyncio.Lock()    # очередь: один запрос за раз (CPU-режим)


# ════════════════════════════════════════════════════════════
#  PREFLIGHT — проверки перед стартом (закрывают риски №2 и №7)
# ════════════════════════════════════════════════════════════

def _available_ram_mb() -> Optional[int]:
    """Свободная RAM в МБ (Linux). None если определить не удалось."""
    # 1) cgroup-лимит (Docker/Kubernetes ограничивают память контейнера)
    for p in (
        "/sys/fs/cgroup/memory.max",                       # cgroup v2
        "/sys/fs/cgroup/memory/memory.limit_in_bytes",     # cgroup v1
    ):
        try:
            with open(p) as fh:
                val = fh.read().strip()
            if val and val != "max":
                limit = int(val) // (1024 * 1024)
                if 0 < limit < 1_000_000:   # игнор «безлимита» (огромное число)
                    return limit
        except Exception:
            pass
    # 2) физическая память хоста
    try:
        with open("/proc/meminfo") as fh:
            for line in fh:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) // 1024
    except Exception:
        pass
    return None


def _preflight():
    """Проверяет окружение. Падает с понятной ошибкой, а не молча в рантайме."""
    # Риск №7: ffmpeg обязателен для faster-whisper (декодирование аудио)
    if shutil.which("ffmpeg") is None:
        raise RuntimeError(
            "❌ ffmpeg не найден в PATH. faster-whisper не сможет читать аудио.\n"
            "   Установи: apt-get install -y ffmpeg  (или используй Dockerfile.issai)"
        )
    log.info("✅ ffmpeg найден")

    # Риск №2: мало RAM → тихий OOM-kill. Лучше упасть сразу с объяснением.
    ram = _available_ram_mb()
    if ram is not None:
        if ram < MIN_RAM_MB:
            raise RuntimeError(
                f"❌ Недостаточно RAM: доступно {ram}MB, нужно минимум {MIN_RAM_MB}MB.\n"
                f"   whisper-large-v3-turbo (int8) ест ~2.5GB. Возьми VPS с 8GB RAM\n"
                f"   или подними порог: ISSAI_MIN_RAM_MB=<меньше> (рискуешь OOM)."
            )
        log.info(f"✅ RAM: {ram}MB доступно (порог {MIN_RAM_MB}MB)")
    else:
        log.warning("⚠️  Не удалось определить объём RAM — пропускаю проверку")


# ════════════════════════════════════════════════════════════
#  ЗАГРУЗКА МОДЕЛИ (закрывает риск №1: авто-конвертация в CT2)
# ════════════════════════════════════════════════════════════

def _is_ct2_dir(path: str) -> bool:
    """CT2-модель = локальная папка с файлом model.bin."""
    return os.path.isdir(path) and os.path.exists(os.path.join(path, "model.bin"))


def _convert_to_ct2(model_id: str) -> str:
    """
    Конвертирует transformers-чекпойнт в формат CTranslate2.
    faster-whisper умеет грузить только CT2. Многие казахские модели на HF
    выложены как обычные PyTorch-чекпойнты → конвертим один раз и кэшируем.
    Возвращает путь к готовой CT2-папке.
    """
    safe_name = model_id.replace("/", "__")
    out_dir   = os.path.join(CT2_CACHE_DIR, safe_name)

    if _is_ct2_dir(out_dir):
        log.info(f"CT2-модель уже в кэше: {out_dir}")
        return out_dir

    try:
        from ctranslate2.converters import TransformersConverter
    except ImportError:
        raise RuntimeError(
            "Модель не в формате CT2, а ctranslate2/transformers не установлены "
            "для конвертации. Запусти: pip install -r requirements-issai.txt"
        )

    # Квантование при конвертации: для CPU int8, для GPU float16
    quant = COMPUTE_TYPE if COMPUTE_TYPE in ("int8", "int8_float16", "float16", "float32") else "int8"

    log.info(f"Конвертация {model_id} → CT2 ({quant}). Это разовая операция, ~1-3 мин...")
    os.makedirs(CT2_CACHE_DIR, exist_ok=True)
    t0 = time.time()
    converter = TransformersConverter(
        model_id,
        copy_files=["tokenizer.json", "preprocessor_config.json"],
    )
    converter.convert(out_dir, quantization=quant, force=True)
    log.info(f"Конвертация готова за {time.time()-t0:.1f}с → {out_dir}")
    return out_dir


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

    common = dict(
        device=DEVICE,
        compute_type=COMPUTE_TYPE,
        cpu_threads=int(os.getenv("ISSAI_THREADS", 4)),
        num_workers=1,
    )

    log.info(f"Загрузка модели {MODEL_ID} (device={DEVICE}, compute={COMPUTE_TYPE})...")
    t0 = time.time()

    # Если явно указали локальную CT2-папку — грузим напрямую.
    if _is_ct2_dir(MODEL_ID):
        _model = WhisperModel(MODEL_ID, **common)
        log.info(f"Модель (CT2 локально) загружена за {time.time()-t0:.1f}с")
        return

    # Иначе: пробуем загрузить как есть (вдруг репозиторий уже в CT2-формате),
    # при неудаче — конвертим из transformers и грузим из кэша.
    try:
        _model = WhisperModel(
            MODEL_ID,
            download_root=os.getenv("ISSAI_CACHE_DIR", None),
            **common,
        )
        log.info(f"Модель загружена напрямую за {time.time()-t0:.1f}с")
        return
    except Exception as e:
        log.warning(f"Прямая загрузка не удалась ({e}). Пробую конвертацию в CT2...")

    ct2_dir = _convert_to_ct2(MODEL_ID)
    _model = WhisperModel(ct2_dir, **common)
    log.info(f"Модель (после конвертации) загружена за {time.time()-t0:.1f}с")


@asynccontextmanager
async def lifespan(app: FastAPI):
    _preflight()
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

    # Нормализуем код языка.
    # "auto"/"" → None: модель сама определяет язык. КРИТИЧНО: жёсткий "kk"
    # ломает русскую речь (модель казахская) — русский мат превращается в кашу.
    lang = (language or "auto").split("-")[0].lower()
    whisper_lang = lang if lang in ("kk", "ru", "en") else None

    t0 = time.time()
    log.info(f"Запрос: {len(audio_bytes)/1024:.0f}KB | lang={whisper_lang or 'auto'} | denoise={DENOISE}")

    # Одновременно работает только один запрос (CPU ограничение)
    async with _model_lock:
        result_text, result_lang, total_dur, num_segs, audio_dur = await asyncio.get_event_loop().run_in_executor(
            None, _run_inference, audio_bytes, whisper_lang
        )

    elapsed = time.time() - t0
    log.info(
        f"Готово: {len(result_text)} симв | speech_dur={total_dur:.1f}с "
        f"| audio_dur={audio_dur:.1f}с | segs={num_segs} | elapsed={elapsed:.1f}с "
        f"| lang={result_lang} | {result_text[:80]!r}"
    )

    return {
        "text":           result_text,
        "language":       result_lang,
        "duration":       round(total_dur, 2),   # длительность распознанной речи
        "audio_duration": round(audio_dur, 2),   # длина исходного аудио (для детекта мусора)
        "segments":       num_segs,
        "elapsed":        round(elapsed, 2),
    }


def _decode_wav(audio_bytes: bytes):
    """
    Декодирует WAV (16-бит PCM, моно) в float32-массив [-1..1] и его длину в сек.
    Возвращает (samples, sample_rate, duration) или (None, 0, 0.0) если не WAV/не моно.
    Нужно для денойза: faster-whisper умеет принимать numpy-массив напрямую.
    """
    try:
        import wave as _wave
        import numpy as _np
        with _wave.open(io.BytesIO(audio_bytes), "rb") as wf:
            if wf.getsampwidth() != 2:
                return None, 0, 0.0
            nch = wf.getnchannels()
            sr  = wf.getframerate()
            nfr = wf.getnframes()
            raw = wf.readframes(nfr)
        a = _np.frombuffer(raw, dtype=_np.int16).astype(_np.float32) / 32768.0
        if nch > 1:
            a = a.reshape(-1, nch).mean(axis=1)
        return a, sr, (len(a) / sr if sr else 0.0)
    except Exception as e:
        log.warning(f"WAV-декод не удался (пойдёт через ffmpeg, без денойза): {e}")
        return None, 0, 0.0


def _denoise(samples, sr):
    """Подавление стационарного шума (аппарат за кассой). Без падений."""
    try:
        import noisereduce as nr
        out = nr.reduce_noise(y=samples, sr=sr, stationary=True, prop_decrease=DENOISE_PROP)
        # лёгкая нормализация громкости после чистки
        import numpy as _np
        peak = float(_np.abs(out).max()) or 1.0
        return (out / peak * 0.7).astype("float32")
    except Exception as e:
        log.warning(f"Денойз не удался — распознаю как есть: {e}")
        return samples


def _run_inference(audio_bytes: bytes, language: Optional[str]) -> tuple:
    """
    Синхронная инференция. Запускается в executor, не блокирует event loop.
    Возвращает (text, language, total_duration, num_segments, audio_duration).
    """
    # Пытаемся декодировать WAV для денойза. Получилось → чистим и кормим массивом.
    # Не WAV / не 16-бит → fallback на ffmpeg (BytesIO), без денойза.
    samples, sr, audio_dur = _decode_wav(audio_bytes)
    if samples is not None and sr == 16000:
        if DENOISE:
            samples = _denoise(samples, sr)
        audio_input = samples
    else:
        audio_input = io.BytesIO(audio_bytes)

    segments, info = _model.transcribe(
        audio_input,
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
        # ── Анти-галлюцинация (главный фикс) ──────────────────────
        # НЕ цепляться за предыдущий текст: иначе одна ошибка на тихом
        # участке тянет за собой выдуманный «связный» бред дальше.
        condition_on_previous_text=False,
        # Жадная декодировка без температурного фолбэка — детерминированно,
        # меньше выдумок (фолбэк на t>0 как раз генерит «правдоподобный» бред).
        temperature=0.0,
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
    # audio_dur=0 если шло через ffmpeg — подставим длительность речи как приближение
    return result_text, result_lang, total_dur, num_segs, (audio_dur or total_dur)


if __name__ == "__main__":
    uvicorn.run(
        "issai_worker:app",
        host="0.0.0.0",
        port=PORT,
        workers=NUM_WORKERS,
        log_level="info",
    )
