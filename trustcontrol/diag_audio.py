#!/usr/bin/env python3
# ════════════════════════════════════════════════════════════
#  diag_audio.py — диагностика аудио конкретной точки
#
#  Что делает:
#    1. Находит точку по имени в БД
#    2. Скачивает N последних WAV из R2 (только записи с s3_key)
#    3. Выводит технические параметры каждого: RMS, dBFS, частота,
#       битность, длина, пиковый уровень, проходит ли RMS-фильтр
#
#  Запуск (на VPS в папке trustcontrol/):
#    python diag_audio.py --name Sportik
#    python diag_audio.py --name Sportik --n 10
#    python diag_audio.py --name Sportik --sravnenie   (+ прогоняет sravnenie.py)
#
#  Требует env: DATABASE_URL, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY,
#               S3_BUCKET, S3_ENDPOINT_URL  (берёт из .env.prod)
# ════════════════════════════════════════════════════════════

import argparse
import asyncio
import io
import math
import os
import sys
import warnings
import wave

# ── Загрузка .env.prod если есть ──────────────────────────────────────────────
_env_file = os.path.join(os.path.dirname(__file__), ".env.prod")
if os.path.exists(_env_file):
    for line in open(_env_file):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())


def _rms(raw: bytes) -> float:
    try:
        import numpy as np
        s = (
            __import__("numpy").frombuffer(raw, dtype=__import__("numpy").int16)
            .astype(float)
        )
        return float(math.sqrt((s ** 2).mean())) if len(s) else 0.0
    except Exception:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                import audioop
                return float(audioop.rms(raw, 2))
        except Exception:
            return 0.0


def _analyze_wav(wav_bytes: bytes, name: str, rms_threshold: int) -> dict:
    try:
        with wave.open(io.BytesIO(wav_bytes)) as wf:
            sr       = wf.getframerate()
            ch       = wf.getnchannels()
            sw       = wf.getsampwidth()
            frames   = wf.getnframes()
            raw      = wf.readframes(frames)
        dur = frames / sr if sr else 0.0
        rms = _rms(raw) if sw == 2 else 0.0

        try:
            import numpy as np
            s = np.frombuffer(raw, dtype=np.int16).astype(float)
            peak = float(np.abs(s).max()) if len(s) else 0.0
        except Exception:
            peak = 0.0

        db_rms  = 20 * math.log10(rms  / 32768) if rms  > 1 else -99.0
        db_peak = 20 * math.log10(peak / 32768) if peak > 1 else -99.0
        passes  = rms >= rms_threshold

        # RMS ПОСЛЕ нормализации — то, что реально уйдёт в STT после фикса.
        # Прогоняем через ту же _normalize_for_stt, что и прод.
        try:
            from backend.services.audio_analyzer import _normalize_for_stt
            norm_bytes = _normalize_for_stt(wav_bytes)
            with wave.open(io.BytesIO(norm_bytes)) as wf2:
                raw2 = wf2.readframes(wf2.getnframes())
            rms_after = _rms(raw2) if sw == 2 else rms
        except Exception:
            rms_after = rms  # функция недоступна (старый код) — считаем без изменения
        db_after = 20 * math.log10(rms_after / 32768) if rms_after > 1 else -99.0
        gain     = rms_after / rms if rms > 0 else 1.0

        return {
            "name":      name,
            "size_kb":   len(wav_bytes) // 1024,
            "dur":       dur,
            "sr":        sr,
            "ch":        ch,
            "bits":      sw * 8,
            "rms":       rms,
            "db_rms":    db_rms,
            "db_peak":   db_peak,
            "passes":    passes,
            "rms_after": rms_after,
            "db_after":  db_after,
            "gain":      gain,
        }
    except Exception as e:
        return {"name": name, "error": str(e)}


def _color(ok: bool, s: str) -> str:
    if not sys.stdout.isatty():
        return s
    return ("\033[32m" if ok else "\033[31m") + s + "\033[0m"


def _print_row(r: dict, rms_threshold: int):
    if "error" in r:
        print(f"  {r['name']:40s}  ОШИБКА: {r['error']}")
        return
    ok   = r["passes"]
    flag = _color(ok, "✓ PASS" if ok else "✗ НИЖЕ ПОРОГА")
    db_s = _color(ok, f"{r['db_rms']:+6.1f}")
    gain = r.get("gain", 1.0)
    # Показываем ДО → ПОСЛЕ нормализации + во сколько раз усилено
    after_s = f"{r.get('db_after', r['db_rms']):+6.1f}"
    gain_s  = f"x{gain:.1f}" if gain > 1.05 else "—"
    print(
        f"  {r['name']:36s}  "
        f"{r['dur']:5.1f}с  "
        f"{r['sr']}Hz/{r['bits']}b  "
        f"ДО:{db_s}dBFS → ПОСЛЕ:{after_s}dBFS  усил:{gain_s:>5}  "
        f"{flag}"
    )


async def _run(location_name: str, n: int, run_sravnenie: bool):
    # ── импорты после установки env ──────────────────────────
    import sqlalchemy as sa
    from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
    from sqlalchemy.orm import sessionmaker

    db_url = os.environ.get("DATABASE_URL", "")
    if not db_url:
        print("ERROR: DATABASE_URL не задан")
        sys.exit(1)

    # asyncpg не любит sslmode в URL
    import re
    db_url = re.sub(r"\?sslmode=\w+", "", db_url)
    db_url = re.sub(r"&sslmode=\w+", "", db_url)
    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql+asyncpg://", 1)
    if db_url.startswith("postgresql://") and "+asyncpg" not in db_url:
        db_url = db_url.replace("postgresql://", "postgresql+asyncpg://", 1)

    engine = create_async_engine(db_url, echo=False)
    Session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with Session() as sess:
        # Найти точку по имени (частичное совпадение)
        from sqlalchemy import text
        row = await sess.execute(
            text("SELECT id, name FROM locations WHERE LOWER(name) LIKE :pat LIMIT 1"),
            {"pat": f"%{location_name.lower()}%"},
        )
        loc = row.fetchone()
        if not loc:
            print(f"Точка с именем '{location_name}' не найдена в БД.")
            sys.exit(1)
        loc_id, loc_name = loc
        print(f"\nТочка: '{loc_name}' (id={loc_id})")

        # Последние N отчётов с s3_key (колонка времени = timestamp;
        # s3_deleted_at IS NULL — файл ещё лежит в R2, не удалён очисткой)
        rows = await sess.execute(
            text(
                "SELECT id, s3_key, timestamp FROM reports "
                "WHERE location_id=:lid AND s3_key IS NOT NULL AND s3_key != '' "
                "AND s3_deleted_at IS NULL "
                "ORDER BY timestamp DESC LIMIT :n"
            ),
            {"lid": loc_id, "n": n},
        )
        reports = rows.fetchall()

    if not reports:
        print("Нет отчётов с аудио в R2. Проверь: S3_BUCKET задан? Точка присылает аудио?")
        sys.exit(0)

    print(f"Найдено {len(reports)} записей с s3_key (последние {n}).\n")

    # ── Скачать WAV из R2 ──────────────────────────────────────────────────
    from backend.services.storage import _build_s3_client
    from backend.config import settings
    s3 = _build_s3_client()

    rms_threshold = settings.RMS_SILENCE_THRESHOLD
    target = getattr(settings, "AUDIO_NORMALIZE_TARGET_RMS", 0)
    print(f"RMS-порог тишины: {rms_threshold} (≈{20*math.log10(rms_threshold/32768):+.1f} dBFS)  |  "
          f"Цель нормализации: {target} "
          f"({'ВЫКЛ' if not target else f'≈{20*math.log10(target/32768):+.1f} dBFS'})\n")
    print(f"  {'Файл':36s}  {'Длит':>5}  {'Формат':>10}  "
          f"{'Громкость ДО→ПОСЛЕ норм.':^30}  Фильтр")
    print("  " + "-" * 108)

    wav_files = []
    for rep_id, s3_key, ts in reports:
        try:
            resp = s3.get_object(Bucket=settings.S3_BUCKET, Key=s3_key)
            wav_bytes = resp["Body"].read()
            short_name = s3_key.split("/")[-1]
            info = _analyze_wav(wav_bytes, short_name, rms_threshold)
            _print_row(info, rms_threshold)
            wav_files.append((rep_id, s3_key, wav_bytes, info))
        except Exception as e:
            print(f"  {'?' * 40}  ОШИБКА скачивания {s3_key}: {e}")

    # ── Итоговая статистика ────────────────────────────────────────────────
    valid = [i for _, _, _, i in wav_files if "error" not in i]
    if valid:
        db_vals = [i["db_rms"] for i in valid if i["db_rms"] > -90]
        avg_db  = sum(db_vals) / len(db_vals) if db_vals else -99
        fails   = sum(1 for i in valid if not i["passes"])
        print()
        print(f"  Средний уровень: {avg_db:+.1f} dBFS  "
              f"| Не прошли RMS-фильтр: {fails}/{len(valid)}")
        if avg_db < -35:
            print()
            print("  ⚠️  ДИАГНОЗ: аудио тихое (< -35 dBFS). Это граничная зона —")
            print("      gpt-4o-transcribe может транскрибировать нестабильно.")
            print("      Рекомендация: нормализация перед STT (см. фикс ниже).")
        elif avg_db < -25:
            print()
            print("  ⚠️  ДИАГНОЗ: аудио умеренно тихое (-35..-25 dBFS).")
            print("      gpt-4o-transcribe обычно справляется, но на зашумлённом")
            print("      сигнале возможны проблемы. Нормализация поможет.")
        else:
            print()
            print("  ✓  Уровень громкости нормальный (> -25 dBFS).")
            print("     Если транскрипты всё равно плохие — проблема не в громкости,")
            print("     а в шуме, акценте или позиции микрофона.")

    # ── Опциональный прогон sravnenie.py ──────────────────────────────────
    if run_sravnenie and wav_files:
        import subprocess, tempfile
        print("\n" + "═" * 64)
        print("  ПРОГОН sravnenie.py (первые 3 файла)")
        print("═" * 64)
        for rep_id, s3_key, wav_bytes, info in wav_files[:3]:
            if "error" in info:
                continue
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                f.write(wav_bytes)
                tmp_path = f.name
            print(f"\n>>> {s3_key.split('/')[-1]}")
            subprocess.run(
                [sys.executable, "sravnenie.py", tmp_path],
                cwd=os.path.dirname(__file__),
            )
            os.unlink(tmp_path)


def main():
    ap = argparse.ArgumentParser(
        description="Диагностика аудио точки: скачивает WAV из R2 и анализирует уровень сигнала."
    )
    ap.add_argument("--name", required=True, help="Имя точки (или часть имени) — ищется в БД")
    ap.add_argument("--n", type=int, default=5, help="Сколько последних записей (по умолчанию 5)")
    ap.add_argument(
        "--sravnenie", action="store_true",
        help="Дополнительно прогнать sravnenie.py на первых 3 файлах"
    )
    args = ap.parse_args()
    asyncio.run(_run(args.name, args.n, args.sravnenie))


if __name__ == "__main__":
    main()
