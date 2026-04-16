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
_parser.add_argument("--compress-format", default="mp3",
                     choices=["wav", "mp3"],
                     help="Формат сжатия: mp3 (64kbps, требует ffmpeg) или wav (8kHz, без зависимостей)")
_parser.add_argument("--device", default=None,
                     help="Индекс или часть названия микрофона (см. --list-devices). "
                          "По умолчанию — системный микрофон по умолчанию.")
_parser.add_argument("--list-devices", action="store_true",
                     help="Показать все доступные устройства записи и выйти")
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
COMPRESS_FORMAT  = _args.compress_format
DEVICE_ARG       = _args.device      # None = системный по умолчанию
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
#  УСТРОЙСТВО ЗАПИСИ: ВЫБОР И ЗАЩИТА ОТ LOOPBACK
# ════════════════════════════════════════════════════════════

# Ключевые слова устройств которые захватывают системный звук
# (колонки, видео, музыка) — а не настоящий микрофон.
# При совпадении monitor выдаёт предупреждение и предлагает альтернативу.
_LOOPBACK_KEYWORDS = {
    "stereo mix", "what u hear", "what you hear",
    "loopback", "wasapi", "virtual", "cable output",
    "vb-audio", "vb audio", "mix", "output", "speaker",
    "soundflower", "blackhole", "voicemeeter",
}


def _list_devices(pa: "pyaudio.PyAudio"):
    """Печатает таблицу всех устройств ввода и выходит."""
    print("\n" + "=" * 60)
    print("  Доступные устройства записи (--device <ИНДЕКС>):")
    print("=" * 60)
    found_any = False
    for i in range(pa.get_device_count()):
        info = pa.get_device_info_by_index(i)
        if info.get("maxInputChannels", 0) < 1:
            continue
        name   = info["name"]
        marker = "  ← РЕКОМЕНДУЕТСЯ" if _is_real_mic(name) else ""
        warn   = "  ⚠ LOOPBACK (захватывает системный звук!)" if _is_loopback(name) else ""
        print(f"  [{i:2d}]  {name}{marker}{warn}")
        found_any = True
    if not found_any:
        print("  Устройства не найдены.")
    print("=" * 60)
    print("  Пример: --device 1   или   --device \"USB Mic\"\n")


def _is_loopback(name: str) -> bool:
    nl = name.lower()
    return any(kw in nl for kw in _LOOPBACK_KEYWORDS)


def _is_real_mic(name: str) -> bool:
    nl = name.lower()
    return any(kw in nl for kw in ("mic", "microphone", "микрофон", "usb", "headset", "гарнитур"))


def _resolve_device_index(pa: "pyaudio.PyAudio", arg: str | None) -> int | None:
    """
    Преобразует аргумент --device в индекс PyAudio.
      None  → системное устройство по умолчанию
      "2"   → индекс 2
      "USB" → первое устройство с "USB" в названии
    """
    if arg is None:
        return None
    if arg.isdigit():
        return int(arg)
    arg_l = arg.lower()
    for i in range(pa.get_device_count()):
        info = pa.get_device_info_by_index(i)
        if info.get("maxInputChannels", 0) > 0 and arg_l in info["name"].lower():
            return i
    log.warning(f"Устройство '{arg}' не найдено — используется системный микрофон")
    return None


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


def _compress_wav_fallback(wav_bytes: bytes) -> bytes:
    """WAV 16 kHz → 8 kHz (прореживание 2:1). Не требует зависимостей."""
    try:
        buf_in = io.BytesIO(wav_bytes)
        with wave.open(buf_in, "rb") as wf:
            pcm = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16)
        downsampled = pcm[::2]
        buf_out = io.BytesIO()
        with wave.open(buf_out, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(8000)
            wf.writeframes(downsampled.tobytes())
        return buf_out.getvalue()
    except Exception as e:
        log.warning(f"WAV-сжатие не удалось: {e}")
        return wav_bytes


def compress_audio(wav_bytes: bytes) -> tuple:
    """
    Сжимает аудио перед отправкой.
    Возвращает: (bytes, content_type, filename)

    Приоритет: MP3 64kbps (pydub + ffmpeg) → WAV 8kHz (без зависимостей).
    MP3 64kbps экономит 75-85% трафика по сравнению с WAV 16kHz.

    Установка ffmpeg: https://ffmpeg.org/download.html (добавить в PATH)
    """
    if not COMPRESS:
        return wav_bytes, "audio/wav", "audio.wav"

    if COMPRESS_FORMAT == "mp3":
        try:
            from pydub import AudioSegment
            audio = AudioSegment.from_wav(io.BytesIO(wav_bytes))
            buf   = io.BytesIO()
            audio.export(buf, format="mp3", bitrate="64k")
            result = buf.getvalue()
            log.debug(f"MP3 64k: {len(wav_bytes)//1024}kB → {len(result)//1024}kB")
            return result, "audio/mpeg", "audio.mp3"
        except ImportError:
            log.warning("pydub не установлен (pip install pydub) — используется WAV 8kHz")
        except Exception as e:
            log.warning(f"MP3 сжатие не удалось ({e}) — используется WAV 8kHz")

    # Fallback: WAV 16→8 kHz
    result = _compress_wav_fallback(wav_bytes)
    log.debug(f"WAV 8kHz: {len(wav_bytes)//1024}kB → {len(result)//1024}kB")
    return result, "audio/wav", "audio.wav"


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
        audio_bytes, content_type, filename = compress_audio(wav_bytes)
        r = _post(
            data={"api_key": API_KEY},
            files={"audio": (filename, audio_bytes, content_type)},
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

def _open_stream(pa: pyaudio.PyAudio, device_index: int | None = None,
                 retry_interval: int = 5) -> pyaudio.Stream:
    """
    Открывает аудио-поток на указанном устройстве.
    Если device_index=None — используется системный микрофон по умолчанию.
    При ошибке повторяет попытки каждые retry_interval секунд.
    """
    # Предупреждаем если выбранное или дефолтное устройство похоже на loopback
    try:
        idx = device_index if device_index is not None else pa.get_default_input_device_info()["index"]
        dev_name = pa.get_device_info_by_index(idx)["name"]
        if _is_loopback(dev_name):
            log.error("=" * 60)
            log.error(f"⚠️  ВНИМАНИЕ: устройство '{dev_name}'")
            log.error("   похоже на системный захват звука (Stereo Mix / Loopback)!")
            log.error("   Оно будет записывать музыку и видео с компьютера,")
            log.error("   а НЕ живой разговор на кассе.")
            log.error("   Подключите USB-микрофон и укажите: --device <ИНДЕКС>")
            log.error("   Список устройств:  monitor.py --list-devices")
            log.error("=" * 60)
        else:
            log.info(f"Микрофон: [{idx}] {dev_name}")
    except Exception:
        pass

    attempt = 0
    while True:
        try:
            kwargs = dict(
                rate=SAMPLE_RATE,
                channels=1,
                format=pyaudio.paInt16,
                input=True,
                frames_per_buffer=FRAME_SIZE,
            )
            if device_index is not None:
                kwargs["input_device_index"] = device_index
            stream = pa.open(**kwargs)
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

    if _args.list_devices:
        _list_devices(pa)
        pa.terminate()
        sys.exit(0)

    device_index = _resolve_device_index(pa, DEVICE_ARG)
    stream = _open_stream(pa, device_index=device_index)

    mode        = "local faster-whisper" if LOCAL_WHISPER else f"сервер ({SERVER_URL})"
    compress_info = f"{COMPRESS_FORMAT.upper()} 64kbps" if COMPRESS and COMPRESS_FORMAT == "mp3" else ("WAV 8kHz" if COMPRESS else "выкл")
    log.info(f"Мониторинг запущен | Транскрипция: {mode} | Сжатие: {compress_info}")

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
        clean   = denoise(voiced)
        raw_wav = frames_to_wav(clean)
        process_segment(raw_wav)
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
                    stream = _open_stream(pa, device_index=device_index)
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
