"""
╔══════════════════════════════════════════════════════════════╗
║     TrustControl — Скрипт на кассе                           ║
║     Записывает звук и отправляет на сервер                   ║
╚══════════════════════════════════════════════════════════════╝

УСТАНОВКА (базовая):
  py -3.13 -m pip install -r requirements-monitor.txt

УСТАНОВКА (с локальным Whisper, бесплатная транскрипция):
  py -3.13 -m pip install -r requirements-monitor.txt faster-whisper

ЗАПУСК:
  py -3.13 monitor.py --api-url https://aspanlab-1.onrender.com --api-key ВАШ_КЛЮЧ

ЗАПУСК (локальная транскрипция, бесплатно):
  py -3.13 monitor.py --api-url https://... --api-key ВАШ_КЛЮЧ --local-whisper

АВТОЗАПУСК Windows:
  Дважды кликни на run.bat (не забудь заполнить API_KEY внутри)
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
                     help="Адрес сервера (например https://aspanlab-1.onrender.com)")
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
                     help="Язык речи: ru, kk (по умолчанию — автоопределение)")
_parser.add_argument("--compress", action="store_true", default=True,
                     help="Сжимать аудио перед отправкой (по умолчанию включено)")
_args = _parser.parse_args()

# ════════════════════════════════════════════════════════════
#  НАСТРОЙКИ
# ════════════════════════════════════════════════════════════

SERVER_URL    = _args.api_url.rstrip("/")
API_KEY       = _args.api_key
LOCAL_WHISPER = _args.local_whisper
WHISPER_MODEL = _args.whisper_model
LANGUAGE      = _args.language
COMPRESS      = _args.compress

VAD_LEVEL        = _args.vad_level
SILENCE_SECONDS  = _args.silence
MAX_MINUTES      = _args.max_minutes
SAMPLE_RATE      = 16000
FRAME_DURATION   = 30          # ms

FRAME_SIZE    = int(SAMPLE_RATE * FRAME_DURATION / 1000)
SILENCE_LIMIT = int(SILENCE_SECONDS * 1000 / FRAME_DURATION)
MAX_FRAMES    = int(MAX_MINUTES * 60 * 1000 / FRAME_DURATION)
FAILS_DIR     = Path(__file__).parent / "fails"
FAILS_DIR.mkdir(exist_ok=True)

# Коды ошибок PyAudio при отключении микрофона
_MIC_DISCONNECT_ERRORS = {-9988, -9985, -9999, -9986, -9981}

# ════════════════════════════════════════════════════════════
#  ЛОГИРОВАНИЕ
# ════════════════════════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("monitor.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger("monitor")

# ════════════════════════════════════════════════════════════
#  ПРОВЕРКА КОНФИГУРАЦИИ
# ════════════════════════════════════════════════════════════

_ok = True
if "localhost" in SERVER_URL:
    log.error("=" * 60)
    log.error("ОШИБКА: SERVER_URL указывает на localhost!")
    log.error(f"  Сейчас: {SERVER_URL}")
    log.error("  Нужно:  --api-url https://aspanlab-1.onrender.com")
    log.error("=" * 60)
    _ok = False

if not API_KEY.strip():
    log.error("=" * 60)
    log.error("ОШИБКА: API_KEY не задан!")
    log.error("  Сайт → Точки → Создайте точку → скопируйте ключ")
    log.error("  Затем: --api-key ВАШ_КЛЮЧ")
    log.error("=" * 60)
    _ok = False

if not _ok:
    sys.exit(1)

# ════════════════════════════════════════════════════════════
#  FASTER-WHISPER (опционально)
# ════════════════════════════════════════════════════════════

_whisper_model_instance = None
if LOCAL_WHISPER:
    try:
        from faster_whisper import WhisperModel
        log.info(f"Загружаю faster-whisper модель '{WHISPER_MODEL}'...")
        _whisper_model_instance = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
        log.info("faster-whisper готов — транскрипция локально (бесплатно).")
    except ImportError:
        log.error("ОШИБКА: pip install faster-whisper")
        sys.exit(1)


# ════════════════════════════════════════════════════════════
#  АУДИО: ЗАХВАТ И ОБРАБОТКА
# ════════════════════════════════════════════════════════════

def frames_to_wav(frames: list[bytes]) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(b"".join(frames))
    return buf.getvalue()


def compress_wav(wav_bytes: bytes) -> bytes:
    """
    Сжимает WAV: 16 kHz → 8 kHz (трафик сокращается вдвое).
    Голос разборчив вплоть до 4 kHz, качество не страдает.
    """
    if not COMPRESS:
        return wav_bytes
    try:
        buf_in = io.BytesIO(wav_bytes)
        with wave.open(buf_in, "rb") as wf:
            pcm = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16)
        # Прореживание 2:1 — берём каждый второй отсчёт
        downsampled = pcm[::2]
        buf_out = io.BytesIO()
        with wave.open(buf_out, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(8000)
            wf.writeframes(downsampled.tobytes())
        compressed = buf_out.getvalue()
        log.debug(f"Сжатие: {len(wav_bytes)//1024}kB → {len(compressed)//1024}kB")
        return compressed
    except Exception as e:
        log.warning(f"Сжатие не удалось: {e}")
        return wav_bytes


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
    """Транскрипция через faster-whisper локально."""
    if not _whisper_model_instance:
        return None
    try:
        segments, info = _whisper_model_instance.transcribe(
            io.BytesIO(wav_bytes), language=LANGUAGE, beam_size=5, vad_filter=True,
        )
        text = " ".join(seg.text.strip() for seg in segments).strip()
        if not text or len(text) < 3:
            return None
        log.info(f"Транскрипция (local): {text!r} [{info.language}]")
        return text
    except Exception as e:
        log.warning(f"Ошибка faster-whisper: {e}")
        return None


# ════════════════════════════════════════════════════════════
#  ОТПРАВКА НА СЕРВЕР
# ════════════════════════════════════════════════════════════

def _post(data: dict, files: dict | None = None, timeout: int = 60):
    """
    POST на сервер. API-ключ в form-поле (UTF-8), не в заголовке — нет latin-1.
    """
    return requests.post(
        f"{SERVER_URL}/api/reports/submit",
        data=data,
        files=files,
        timeout=timeout,
    )


def send_audio_to_server(wav_bytes: bytes):
    """Отправляем аудио. При ошибке — сохраняем в fails/ для повторной отправки."""
    try:
        r = _post(
            data={"api_key": API_KEY},
            files={"audio": ("audio.wav", wav_bytes, "audio/wav")},
        )
        _handle_response(r, wav_bytes=wav_bytes)
    except requests.exceptions.ConnectionError:
        log.warning("Сервер недоступен (нет сети). Файл сохранён в fails/")
        _save_fail(wav_bytes)
    except requests.exceptions.Timeout:
        log.warning("Сервер не ответил за 60 сек. Файл сохранён в fails/")
        _save_fail(wav_bytes)
    except Exception as e:
        log.warning(f"Ошибка отправки: {e}")
        _save_fail(wav_bytes)


def send_text_to_server(transcript: str):
    """Отправляем готовый транскрипт (режим local-whisper)."""
    try:
        r = _post(data={"api_key": API_KEY, "transcript_text": transcript})
        _handle_response(r)
    except Exception as e:
        log.warning(f"Ошибка отправки текста: {e}")


def _handle_response(r, wav_bytes: bytes | None = None):
    if r.status_code == 200:
        data = r.json()
        status = data.get("status", "ok")
        if status == "queued":
            log.info("В очередь | обработка в фоне")
        else:
            log.info(
                f"Отправлено | тон={data.get('tone')} "
                f"| оценка={data.get('gpt_score') or data.get('score')}"
            )
        _retry_fails()
    elif r.status_code == 401:
        log.error("НЕВЕРНЫЙ API КЛЮЧ! Проверьте --api-key")
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
    """Пересылаем ранее не отправленные файлы."""
    for fpath in sorted(FAILS_DIR.glob("*.wav")):
        try:
            wav_bytes = fpath.read_bytes()
            r = _post(
                data={"api_key": API_KEY},
                files={"audio": ("audio.wav", wav_bytes, "audio/wav")},
                timeout=30,
            )
            if r.status_code == 200:
                fpath.unlink()
                log.info(f"Переслан из fails/: {fpath.name}")
            else:
                break
        except Exception:
            break


def process_segment(wav_bytes: bytes):
    """Обрабатывает один речевой сегмент (в отдельном потоке)."""
    if LOCAL_WHISPER:
        transcript = transcribe_local(wav_bytes)
        if transcript:
            threading.Thread(target=send_text_to_server, args=(transcript,), daemon=True).start()
        else:
            log.info("Речь не распознана (local whisper)")
    else:
        threading.Thread(target=send_audio_to_server, args=(wav_bytes,), daemon=True).start()


# ════════════════════════════════════════════════════════════
#  МИКРОФОН: ОТКРЫТИЕ С ПОВТОРАМИ
# ════════════════════════════════════════════════════════════

def _open_stream(pa: pyaudio.PyAudio, retry_interval: int = 5) -> pyaudio.Stream:
    """
    Открывает аудио-поток. Повторяет попытки каждые retry_interval секунд
    пока микрофон не появится. Безопасен при горячем подключении.
    """
    attempt = 0
    while True:
        try:
            stream = pa.open(
                rate=SAMPLE_RATE,
                channels=1,
                format=pyaudio.paInt16,
                input=True,
                frames_per_buffer=FRAME_SIZE,
            )
            if attempt > 0:
                log.info("Микрофон снова подключён.")
            return stream
        except Exception as e:
            if attempt == 0:
                log.warning(f"Микрофон недоступен: {e}")
                log.warning(f"Жду переподключения (каждые {retry_interval} сек)...")
            attempt += 1
            time.sleep(retry_interval)


# ════════════════════════════════════════════════════════════
#  ОСНОВНОЙ ЦИКЛ
# ════════════════════════════════════════════════════════════

def run():
    vad = webrtcvad.Vad(VAD_LEVEL)
    pa  = pyaudio.PyAudio()
    stream = _open_stream(pa)

    mode = "local faster-whisper" if LOCAL_WHISPER else f"сервер ({SERVER_URL})"
    log.info(f"Мониторинг запущен | Транскрипция: {mode} | Сжатие: {'вкл' if COMPRESS else 'выкл'}")

    voiced    = []
    silence   = 0
    in_speech = False

    def flush(reason=""):
        nonlocal voiced, silence, in_speech
        if not voiced:
            return
        # Минимум 100 кадров ≈ 3 секунды реальной речи
        # Короче — шум, случайный звук или одно слово, не отправляем
        if len(voiced) < 100:
            log.debug(f"Сегмент слишком короткий ({len(voiced)} кадров) — пропускаем")
            voiced = []; silence = 0; in_speech = False
            return
        log.info(f"Обрабатываю сегмент ({reason}), кадров: {len(voiced)}")
        clean     = denoise(voiced)
        raw_wav   = frames_to_wav(clean)
        small_wav = compress_wav(raw_wav)
        process_segment(small_wav)
        voiced = []; silence = 0; in_speech = False

    try:
        while True:
            # ── Читаем фрейм с микрофона ────────────────────────
            try:
                frame = stream.read(FRAME_SIZE, exception_on_overflow=False)

            except OSError as e:
                # Проверяем: это отключение микрофона или обычный overflow?
                err_code = None
                if e.args:
                    # PyAudio упаковывает (errno, message) в args
                    for arg in e.args:
                        if isinstance(arg, int):
                            err_code = arg
                            break

                if err_code in _MIC_DISCONNECT_ERRORS:
                    log.warning(f"Микрофон отключён (код {err_code}). Жду переподключения...")
                    # Сбрасываем буфер — не отправляем неполный сегмент
                    voiced = []; silence = 0; in_speech = False
                    try:
                        stream.stop_stream()
                        stream.close()
                    except Exception:
                        pass
                    time.sleep(2)
                    stream = _open_stream(pa)
                else:
                    # Overflow или другой временный сбой — просто пропускаем
                    log.debug(f"Ошибка чтения (код {err_code}): {e}")
                    time.sleep(0.05)
                continue

            except Exception as e:
                log.warning(f"Ошибка микрофона: {e}")
                time.sleep(0.1)
                continue

            # ── VAD: детектируем речь ────────────────────────────
            try:
                is_speech = vad.is_speech(frame, SAMPLE_RATE)
            except Exception:
                is_speech = False

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
        log.info("Остановлено пользователем.")
        flush("принудительная остановка")
    finally:
        try:
            stream.stop_stream()
            stream.close()
        except Exception:
            pass
        pa.terminate()


# ════════════════════════════════════════════════════════════
#  ТОЧКА ВХОДА
# ════════════════════════════════════════════════════════════

if __name__ == "__main__":
    while True:
        try:
            run()
        except KeyboardInterrupt:
            log.info("Выход.")
            break
        except Exception:
            log.error("Критическая ошибка:\n" + traceback.format_exc())
            log.info("Перезапуск через 5 секунд...")
            time.sleep(5)
