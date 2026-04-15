"""
╔══════════════════════════════════════════════════════════════╗
║     TrustControl — Скрипт на кассе                           ║
║     Записывает звук и отправляет на сервер                   ║
╚══════════════════════════════════════════════════════════════╝

УСТАНОВКА (базовая):
  pip install -r requirements-monitor.txt

УСТАНОВКА (с локальным Whisper, бесплатная транскрипция):
  pip install -r requirements-monitor.txt faster-whisper

ЗАПУСК (транскрипция на сервере):
  python monitor.py --api-url https://aspanlab-1.onrender.com --api-key ВАШ_КЛЮЧ

ЗАПУСК (транскрипция локально, бесплатно):
  python monitor.py --api-url https://aspanlab-1.onrender.com --api-key ВАШ_КЛЮЧ --local-whisper

АВТОЗАПУСК Windows (без окна):
  Переименуй в monitor.pyw и добавь ярлык с аргументами в автозагрузку
"""

import argparse
import io
import sys
import time
import wave
import logging
import threading
import traceback
from pathlib import Path

import numpy as np
import pyaudio
import webrtcvad
import noisereduce as nr
import requests

# ════════════════════════════════════════════════════════════
#  АРГУМЕНТЫ КОМАНДНОЙ СТРОКИ
# ════════════════════════════════════════════════════════════

_parser = argparse.ArgumentParser(description="TrustControl — монитор кассы")
_parser.add_argument("--api-url", default="http://localhost:8000",
                     help="Адрес сервера TrustControl (например https://aspanlab-1.onrender.com)")
_parser.add_argument("--api-key", default="",
                     help="API-ключ точки из личного кабинета")
_parser.add_argument("--vad-level", type=int, default=2,
                     help="Чувствительность VAD 0-3 (по умолчанию 2)")
_parser.add_argument("--silence", type=float, default=2.5,
                     help="Секунд тишины = конец разговора (по умолчанию 2.5)")
_parser.add_argument("--max-minutes", type=int, default=2,
                     help="Максимальная длина сегмента в минутах (по умолчанию 2)")
_parser.add_argument("--local-whisper", action="store_true",
                     help="Транскрибировать локально через faster-whisper (бесплатно)")
_parser.add_argument("--whisper-model", default="small",
                     choices=["tiny", "base", "small", "medium"],
                     help="Размер модели faster-whisper (по умолчанию small)")
_parser.add_argument("--language", default=None,
                     help="Язык речи: ru, kk, auto (по умолчанию auto)")
_args = _parser.parse_args()

# ════════════════════════════════════════════════════════════
#  НАСТРОЙКИ
# ════════════════════════════════════════════════════════════

SERVER_URL    = _args.api_url.rstrip("/")
API_KEY       = _args.api_key
LOCAL_WHISPER = _args.local_whisper
WHISPER_MODEL = _args.whisper_model
LANGUAGE      = _args.language  # None = автоопределение

# ── Дополнительные настройки ─────────────────────────────────
VAD_LEVEL        = _args.vad_level
SILENCE_SECONDS  = _args.silence
MAX_MINUTES      = _args.max_minutes
SAMPLE_RATE      = 16000
FRAME_DURATION   = 30      # ms

# ════════════════════════════════════════════════════════════

FRAME_SIZE    = int(SAMPLE_RATE * FRAME_DURATION / 1000)
SILENCE_LIMIT = int(SILENCE_SECONDS * 1000 / FRAME_DURATION)
MAX_FRAMES    = int(MAX_MINUTES * 60 * 1000 / FRAME_DURATION)
FAILS_DIR     = Path(__file__).parent / "fails"
FAILS_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("monitor.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger("monitor")

# ── Проверка конфигурации при старте ────────────────────────
_CONFIG_OK = True
if "localhost" in SERVER_URL:
    log.error("=" * 60)
    log.error("ОШИБКА: SERVER_URL указывает на localhost!")
    log.error(f"  Текущее значение: {SERVER_URL}")
    log.error("  Укажите реальный адрес сервера:")
    log.error("  --api-url https://aspanlab-1.onrender.com")
    log.error("=" * 60)
    _CONFIG_OK = False

if not API_KEY.strip():
    log.error("=" * 60)
    log.error("ОШИБКА: API_KEY не задан!")
    log.error("  Зайдите на сайт → Точки → Создайте точку → скопируйте ключ")
    log.error("  Затем запустите с аргументом: --api-key ВАШ_РЕАЛЬНЫЙ_КЛЮЧ")
    log.error("=" * 60)
    _CONFIG_OK = False

if not _CONFIG_OK:
    sys.exit(1)

# ── Загрузка faster-whisper если нужно ──────────────────────
_whisper_model_instance = None
if LOCAL_WHISPER:
    try:
        from faster_whisper import WhisperModel
        log.info(f"Загружаю faster-whisper модель '{WHISPER_MODEL}'...")
        _whisper_model_instance = WhisperModel(
            WHISPER_MODEL,
            device="cpu",
            compute_type="int8",
        )
        log.info("faster-whisper загружен. Транскрипция — локально (бесплатно).")
    except ImportError:
        log.error("=" * 60)
        log.error("ОШИБКА: faster-whisper не установлен!")
        log.error("  Установите командой:")
        log.error("  pip install faster-whisper")
        log.error("=" * 60)
        sys.exit(1)


# ════════════════════════════════════════════════════════════
#  ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ════════════════════════════════════════════════════════════

def frames_to_wav(frames: list[bytes]) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(b"".join(frames))
    return buf.getvalue()


def denoise(frames: list[bytes]) -> list[bytes]:
    try:
        pcm = np.frombuffer(b"".join(frames), dtype=np.int16).astype(np.float32)
        clean = nr.reduce_noise(y=pcm, sr=SAMPLE_RATE, stationary=False)
        raw = np.clip(clean, -32768, 32767).astype(np.int16).tobytes()
        size = FRAME_SIZE * 2
        return [raw[i:i+size] for i in range(0, len(raw), size) if len(raw[i:i+size]) == size]
    except Exception as e:
        log.warning(f"Шумоподавление: {e}")
        return frames


def transcribe_local(wav_bytes: bytes) -> str | None:
    """Транскрибирует WAV локально через faster-whisper. Бесплатно."""
    if not _whisper_model_instance:
        return None
    try:
        buf = io.BytesIO(wav_bytes)
        segments, info = _whisper_model_instance.transcribe(
            buf,
            language=LANGUAGE,
            beam_size=5,
            vad_filter=True,
        )
        text = " ".join(seg.text.strip() for seg in segments).strip()
        if not text or len(text) < 3:
            return None
        log.info(f"Транскрипция (local): {text!r} [{info.language}]")
        return text
    except Exception as e:
        log.warning(f"Ошибка faster-whisper: {e}")
        return None


def _post(url: str, data: dict, files: dict | None = None, timeout: int = 30):
    """
    Обёртка над requests.post.
    API-ключ передаётся как form-поле 'api_key' (не заголовок),
    поэтому нет проблем с кодировкой latin-1.
    """
    return requests.post(url, data=data, files=files, timeout=timeout)


def send_audio_to_server(wav_bytes: bytes):
    """Отправляем аудио на сервер. При ошибке — в папку fails/."""
    try:
        r = _post(
            f"{SERVER_URL}/api/reports/submit",
            data={"api_key": API_KEY},
            files={"audio": ("audio.wav", wav_bytes, "audio/wav")},
        )
        _handle_response(r, wav_bytes=wav_bytes)
    except Exception as e:
        log.warning(f"Сервер недоступен: {e}")
        _save_fail(wav_bytes)


def send_text_to_server(transcript: str):
    """Отправляем уже готовый транскрипт (режим local-whisper)."""
    try:
        r = _post(
            f"{SERVER_URL}/api/reports/submit",
            data={"api_key": API_KEY, "transcript_text": transcript},
        )
        _handle_response(r)
    except Exception as e:
        log.warning(f"Сервер недоступен: {e}")


def _handle_response(r, wav_bytes: bytes | None = None):
    if r.status_code == 200:
        data = r.json()
        log.info(
            f"Отправлено | тон={data.get('tone')} "
            f"| оценка={data.get('score')} "
            f"| резюме={data.get('gpt_summary', '')[:60]}"
        )
        _retry_fails()
    else:
        log.warning(f"Сервер вернул {r.status_code}: {r.text[:120]}")
        if wav_bytes:
            _save_fail(wav_bytes)


def _save_fail(wav_bytes: bytes):
    import datetime
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    path = FAILS_DIR / f"{ts}.wav"
    path.write_bytes(wav_bytes)
    log.info(f"Сохранено в fails/: {path.name}")


def _retry_fails():
    for fpath in sorted(FAILS_DIR.glob("*.wav")):
        try:
            wav_bytes = fpath.read_bytes()
            r = _post(
                f"{SERVER_URL}/api/reports/submit",
                data={"api_key": API_KEY},
                files={"audio": ("audio.wav", wav_bytes, "audio/wav")},
            )
            if r.status_code == 200:
                fpath.unlink()
                log.info(f"Переслан из fails/: {fpath.name}")
        except Exception:
            break


def process_segment(wav_bytes: bytes):
    """Обрабатывает один речевой сегмент."""
    if LOCAL_WHISPER:
        transcript = transcribe_local(wav_bytes)
        if transcript:
            threading.Thread(target=send_text_to_server, args=(transcript,), daemon=True).start()
        else:
            log.info("Речь не распознана (local whisper)")
    else:
        threading.Thread(target=send_audio_to_server, args=(wav_bytes,), daemon=True).start()


# ════════════════════════════════════════════════════════════
#  ОСНОВНОЙ ЦИКЛ
# ════════════════════════════════════════════════════════════

def run():
    vad = webrtcvad.Vad(VAD_LEVEL)
    pa  = pyaudio.PyAudio()

    stream = pa.open(
        rate=SAMPLE_RATE,
        channels=1,
        format=pyaudio.paInt16,
        input=True,
        frames_per_buffer=FRAME_SIZE,
    )

    mode = "local faster-whisper" if LOCAL_WHISPER else "сервер"
    log.info(f"Мониторинг запущен. Сервер: {SERVER_URL} | Транскрипция: {mode}")

    voiced   = []
    silence  = 0
    in_speech = False

    def flush(reason=""):
        nonlocal voiced, silence, in_speech
        if not voiced:
            return
        log.info(f"Обрабатываю сегмент ({reason}), кадров: {len(voiced)}")
        clean = denoise(voiced)
        wav   = frames_to_wav(clean)
        process_segment(wav)
        voiced = []; silence = 0; in_speech = False

    try:
        while True:
            try:
                frame = stream.read(FRAME_SIZE, exception_on_overflow=False)
            except Exception as e:
                log.warning(f"Ошибка микрофона: {e}")
                time.sleep(0.1)
                continue

            is_speech = vad.is_speech(frame, SAMPLE_RATE)

            if is_speech:
                voiced.append(frame)
                silence   = 0
                in_speech = True
            elif in_speech:
                voiced.append(frame)
                silence += 1
                if silence >= SILENCE_LIMIT:
                    flush("конец речи")
                elif len(voiced) >= MAX_FRAMES:
                    flush("макс. длина")

    except KeyboardInterrupt:
        log.info("Остановлено.")
    finally:
        stream.stop_stream()
        stream.close()
        pa.terminate()


if __name__ == "__main__":
    while True:
        try:
            run()
        except Exception:
            log.error("Критическая ошибка:\n" + traceback.format_exc())
            time.sleep(5)
