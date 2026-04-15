"""
╔══════════════════════════════════════════════════════════════╗
║     TrustControl — Скрипт на кассе                           ║
║     Записывает звук и отправляет на сервер                   ║
╚══════════════════════════════════════════════════════════════╝

УСТАНОВКА:
  pip install pyaudio webrtcvad noisereduce numpy requests

ЗАПУСК:
  python monitor.py --api-url https://aspanlab-1.onrender.com --api-key ВАШ_КЛЮЧ

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
_parser.add_argument("--api-key", default="ВАШ_API_КЛЮЧ_ТОЧКИ",
                     help="API-ключ точки из личного кабинета")
_parser.add_argument("--vad-level", type=int, default=2,
                     help="Чувствительность VAD 0-3 (по умолчанию 2)")
_parser.add_argument("--silence", type=float, default=2.5,
                     help="Секунд тишины = конец разговора (по умолчанию 2.5)")
_parser.add_argument("--max-minutes", type=int, default=2,
                     help="Максимальная длина сегмента в минутах (по умолчанию 2)")
_args = _parser.parse_args()

# ════════════════════════════════════════════════════════════
#  НАСТРОЙКИ
# ════════════════════════════════════════════════════════════

SERVER_URL = _args.api_url.rstrip("/")
API_KEY    = _args.api_key

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

if API_KEY == "ВАШ_API_КЛЮЧ_ТОЧКИ" or not API_KEY.strip():
    log.error("=" * 60)
    log.error("ОШИБКА: API_KEY не заполнен!")
    log.error("  Зайдите на сайт → Точки → Создайте точку → скопируйте ключ")
    log.error("  Затем запустите скрипт с аргументом:")
    log.error("  --api-key ВАШ_РЕАЛЬНЫЙ_КЛЮЧ")
    log.error("=" * 60)
    _CONFIG_OK = False

try:
    API_KEY.encode("latin-1")
except UnicodeEncodeError:
    log.error("=" * 60)
    log.error("ОШИБКА: API_KEY содержит недопустимые символы!")
    log.error("  API-ключ должен состоять только из латинских букв и цифр.")
    log.error("  Получите правильный ключ на сайте → Точки.")
    log.error("=" * 60)
    _CONFIG_OK = False

if not _CONFIG_OK:
    sys.exit(1)


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


def send_to_server(wav_bytes: bytes):
    """Отправляем аудио на сервер. При ошибке — в папку fails/."""
    try:
        r = requests.post(
            f"{SERVER_URL}/api/reports/submit",
            files={"audio": ("audio.wav", wav_bytes, "audio/wav")},
            headers={"X-API-Key": API_KEY},
            timeout=30,
        )
        if r.status_code == 200:
            data = r.json()
            log.info(f"✅ Отправлено | тон={data.get('tone')} | оценка={data.get('score')}")
            _retry_fails()
        else:
            log.warning(f"Сервер вернул {r.status_code}: {r.text[:100]}")
            _save_fail(wav_bytes)
    except Exception as e:
        log.warning(f"Сервер недоступен: {e}")
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
            r = requests.post(
                f"{SERVER_URL}/api/reports/submit",
                files={"audio": ("audio.wav", wav_bytes, "audio/wav")},
                headers={"X-API-Key": API_KEY},
                timeout=30,
            )
            if r.status_code == 200:
                fpath.unlink()
                log.info(f"Переслан из fails/: {fpath.name}")
        except Exception:
            break


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

    log.info(f"🎙️ Мониторинг запущен. Сервер: {SERVER_URL}")

    voiced   = []
    silence  = 0
    in_speech= False

    def flush(reason=""):
        nonlocal voiced, silence, in_speech
        if not voiced:
            return
        log.info(f"Обрабатываю сегмент ({reason}), кадров: {len(voiced)}")
        clean = denoise(voiced)
        wav   = frames_to_wav(clean)
        threading.Thread(target=send_to_server, args=(wav,), daemon=True).start()
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
