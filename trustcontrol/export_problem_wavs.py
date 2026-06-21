#!/usr/bin/env python3
# ════════════════════════════════════════════════════════════
#  export_problem_wavs.py — выгрузка ПРОБЛЕМНЫХ кассовых записей
#  из прода (Neon БД → R2) в один zip для отправки партнёру STT.
#
#  Партнёр тестит свои модели у себя — ему нужны РЕАЛЬНЫЕ проблемные
#  вавки (казахский / шала-казах / русский), а не наговоренные в микрофон.
#
#  Что делает:
#    1. Берёт из Neon последние записи с аудио (s3_key есть).
#    2. Отбирает проблемные: приоритет — казахские/смешанные (там модели
#       спотыкаются), плюс добор русскими для разнообразия. Пустышки/
#       совсем короткие (<MIN_WORDS слов) пропускает.
#    3. Качает WAV из R2 по ключу (storage.download_bytes).
#    4. Кладёт в папку: NNN_<kk|ru>_r<id>.wav
#    5. Пишет manifest.txt — что НАША система распознала (эталон для
#       сравнения; помечено «может содержать ошибки»).
#    6. Пакует всё в один .zip.
#
#  Запуск (внутри контейнера api):
#    python export_problem_wavs.py
#    python export_problem_wavs.py --days 14 --limit 30
#    python export_problem_wavs.py --only-kazakh        (только каз/смесь)
#
#  Забрать готовый zip на хост:
#    docker compose -f docker-compose.prod.yml --env-file .env.prod \
#        cp api:/app/partner_wavs.zip ./partner_wavs.zip
#
#  Требует env (есть в .env.prod): DATABASE_URL (Neon), S3_*/AWS_* (R2).
# ════════════════════════════════════════════════════════════

import argparse
import asyncio
import os
import sys
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Казахские буквы, которых НЕТ в русском алфавите. Наличие хотя бы одной —
# верный признак казахской/смешанной речи (самые трудные для STT записи).
_KAZAKH_CHARS = set("әғқңөұүһі")


def _has_kazakh(text: str) -> bool:
    return any(ch in _KAZAKH_CHARS for ch in (text or "").lower())


def _word_count(text: str) -> int:
    return len((text or "").split())


def _lang_tag(text: str) -> str:
    return "kk" if _has_kazakh(text) else "ru"


async def main(args) -> int:
    from sqlalchemy import select
    from backend.database import AsyncSessionLocal
    from backend.models.report import Report
    from backend.services.storage import download_bytes
    from datetime import datetime, timedelta

    out_dir = args.out
    os.makedirs(out_dir, exist_ok=True)

    since = datetime.utcnow() - timedelta(days=args.days)

    # Берём с запасом (в 6× от лимита) — потом отфильтруем по содержимому.
    fetch_n = max(args.limit * 6, 60)
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(Report)
            .where(Report.s3_key.isnot(None))
            .where(Report.timestamp >= since)
            .order_by(Report.timestamp.desc())
            .limit(fetch_n)
        )).scalars().all()

    print(f"Найдено записей с аудио за {args.days} дн.: {len(rows)}")

    # ── Отбор проблемных ──
    # Делим на казахские/смешанные (приоритет) и русские (добор).
    kazakh, russian = [], []
    for r in rows:
        if _word_count(r.transcript) < args.min_words:
            continue  # пустышка/обрывок — партнёру неинтересно
        (kazakh if _has_kazakh(r.transcript) else russian).append(r)

    if args.only_kazakh:
        picked = kazakh[:args.limit]
    else:
        # 70% казах/смесь, остаток — русские (для разнообразия)
        n_kk = min(len(kazakh), max(1, int(args.limit * 0.7)))
        n_ru = args.limit - n_kk
        picked = kazakh[:n_kk] + russian[:n_ru]

    if not picked:
        print("⚠ Подходящих записей не нашлось — попробуй увеличить --days.")
        return 1

    print(f"Отобрано: {len(picked)}  (каз/смесь: {sum(_has_kazakh(r.transcript) for r in picked)}, "
          f"рус: {sum(not _has_kazakh(r.transcript) for r in picked)})")
    print(f"Качаю WAV из R2 в {out_dir} …\n")

    manifest = [
        "# ПРОБЛЕМНЫЕ КАССОВЫЕ ЗАПИСИ TrustControl — для теста STT-моделей",
        "# Транскрипт ниже = что РАСПОЗНАЛА наша текущая система (gpt-4o + склейка).",
        "# Это НЕ эталон, а текущий результат — на нём видно где модель ошибается",
        "# (суммы, казахские слова, шала-казах). Сравнивай свой вывод с ним.",
        "#" + "=" * 70,
        "",
    ]

    saved = 0
    for r in picked:
        wav = await download_bytes(r.s3_key)
        if not wav:
            print(f"  ⊘ #{r.id}: не скачалось (ключ {r.s3_key})")
            continue
        saved += 1
        tag = _lang_tag(r.transcript)
        fname = f"{saved:03d}_{tag}_r{r.id}.wav"
        with open(os.path.join(out_dir, fname), "wb") as f:
            f.write(wav)
        dur_note = f"{(r.audio_size_kb or 0)} КБ"
        print(f"  ✓ {fname}  ({dur_note}, {_word_count(r.transcript)} слов, {tag})")
        manifest += [
            f"## {fname}",
            f"   report_id : {r.id}",
            f"   время     : {r.timestamp:%Y-%m-%d %H:%M} UTC",
            f"   точка     : loc{r.location_id}",
            f"   язык(прим): {'казахский/смесь' if tag == 'kk' else 'русский'}",
            f"   размер    : {dur_note}",
            f"   распознано: {(r.transcript or '').strip()}",
            "",
        ]

    if saved == 0:
        print("\n⚠ Ни одного файла не скачалось — проверь S3_*/AWS_* в .env.prod.")
        return 1

    manifest_path = os.path.join(out_dir, "manifest.txt")
    with open(manifest_path, "w", encoding="utf-8") as f:
        f.write("\n".join(manifest))

    # ── Упаковка в zip ──
    zip_path = out_dir.rstrip("/") + ".zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in sorted(os.listdir(out_dir)):
            zf.write(os.path.join(out_dir, name), arcname=name)

    size_mb = os.path.getsize(zip_path) / 1024 / 1024
    print(f"\n✓ Готово: {saved} WAV + manifest.txt")
    print(f"✓ Архив:  {zip_path}  ({size_mb:.1f} МБ)")
    print(f"\nЗабрать на хост:")
    print(f"  docker compose -f docker-compose.prod.yml --env-file .env.prod \\")
    print(f"      cp api:{zip_path} ./partner_wavs.zip")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Выгрузка проблемных кассовых WAV для партнёра STT.")
    ap.add_argument("--days", type=int, default=30, help="за сколько последних дней искать (дефолт 30)")
    ap.add_argument("--limit", type=int, default=20, help="сколько файлов выгрузить (дефолт 20)")
    ap.add_argument("--min-words", type=int, default=5, help="минимум слов в транскрипте (дефолт 5)")
    ap.add_argument("--only-kazakh", action="store_true", help="только казахские/смешанные записи")
    ap.add_argument("--out", default="/app/partner_wavs", help="папка для WAV (дефолт /app/partner_wavs)")
    args = ap.parse_args()
    sys.exit(asyncio.run(main(args)) or 0)
