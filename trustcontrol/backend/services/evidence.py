# ════════════════════════════════════════════════════════════
#  Сервис: Нарезка аудио-доказательств
#
#  При инциденте KASPI_FRAUD или AGGRESSION — вырезаем
#  последние 30 секунд записи через ffmpeg и загружаем в S3.
#  Это компактный «пруф» который можно прослушать в Telegram.
#
#  Требует: ffmpeg в PATH
# ════════════════════════════════════════════════════════════

import logging
import subprocess

from backend.services.storage import upload_evidence as _upload

log = logging.getLogger("evidence")

CLIP_MAX_SEC = 30  # длина клипа-доказательства


def extract_clip(wav_bytes: bytes, duration_sec: int = CLIP_MAX_SEC) -> bytes:
    """
    Вырезает последние `duration_sec` секунд из WAV-записи через ffmpeg.
    При ошибке возвращает исходные байты без изменений.

    ffmpeg флаг -sseof -N: seek на N секунд с конца файла.
    """
    try:
        proc = subprocess.run(
            [
                "ffmpeg",
                "-loglevel", "error",
                "-i", "pipe:0",
                "-sseof", f"-{duration_sec}",
                "-acodec", "pcm_s16le",
                "-ar", "16000",
                "-ac", "1",
                "-f", "wav",
                "pipe:1",
            ],
            input=wav_bytes,
            capture_output=True,
            timeout=30,
        )
        if proc.returncode == 0 and len(proc.stdout) > 512:
            log.debug(
                f"Клип: {len(wav_bytes) // 1024} kB → {len(proc.stdout) // 1024} kB "
                f"(последние {duration_sec}s)"
            )
            return proc.stdout
        log.warning(f"ffmpeg clip rc={proc.returncode}: {proc.stderr[:150].decode(errors='replace')}")
        return wav_bytes
    except FileNotFoundError:
        log.warning("ffmpeg не найден — возвращаем оригинал без нарезки")
        return wav_bytes
    except Exception as e:
        log.warning(f"Ошибка нарезки клипа: {e}")
        return wav_bytes


async def create_evidence_clip(
    wav_bytes: bytes,
    location_id: int,
    report_id: int,
) -> dict:
    """
    Вырезает клип и загружает в S3.
    Возвращает {"s3_url": ..., "sha256": ...} или {}.

    Использует существующий upload_evidence с суффиксом _clip в report_id
    чтобы не перезаписывать оригинальный архив.
    """
    if not wav_bytes:
        return {}
    try:
        clip = extract_clip(wav_bytes)
        # Используем report_id * -1 как маркер клипа (key будет отличаться)
        result = await _upload(clip, location_id, -(report_id))
        return result
    except Exception as e:
        log.error(f"create_evidence_clip loc={location_id} report={report_id}: {e}")
        return {}
