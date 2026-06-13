#!/usr/bin/env python3
# ════════════════════════════════════════════════════════════
#  test_pipeline.py — диагностический прогон ОДНОГО аудиофайла
#  через реальный STT-пайплайн (Шаги 3-4).
#
#  Показывает по шагам: RMS-фильтр, сырой транскрипт, чистый
#  транскрипт, список правок, уверенность, ВРЕМЯ и СТОИМОСТЬ($)
#  каждого шага.
#
#  Запуск:
#    python test_pipeline.py запись.wav
#    python test_pipeline.py запись.wav --glossary "капучино,рожок,Айгуль"
#    python test_pipeline.py запись.wav --threshold 200
#
#  Требует переменные окружения: OPENAI_API_KEY, SECRET_KEY.
#  Использует ТЕ ЖЕ функции что и боевой сервер (audio_analyzer),
#  поэтому числа отражают реальный прод, а не пересказ логики.
# ════════════════════════════════════════════════════════════

import argparse
import asyncio
import io
import os
import sys
import time
import wave

# ── Тарифы OpenAI (USD). Переопределяются через env если цены изменятся ──
RATE_STT_PER_MIN   = float(os.getenv("RATE_STT_PER_MIN", "0.006"))          # gpt-4o-transcribe
RATE_MINI_IN_1M    = float(os.getenv("RATE_MINI_IN_PER_1M", "0.15"))        # gpt-4o-mini вход
RATE_MINI_OUT_1M   = float(os.getenv("RATE_MINI_OUT_PER_1M", "0.60"))       # gpt-4o-mini выход
USD_TO_KZT         = float(os.getenv("USD_TO_KZT", "470"))                  # для справки в ₸

# ── Цвета (минимально, отключаются если не tty) ──────────────
_C = sys.stdout.isatty()
def _b(s):  return f"\033[1m{s}\033[0m"  if _C else s
def _g(s):  return f"\033[32m{s}\033[0m" if _C else s
def _y(s):  return f"\033[33m{s}\033[0m" if _C else s
def _r(s):  return f"\033[31m{s}\033[0m" if _C else s
def _dim(s):return f"\033[2m{s}\033[0m"  if _C else s


def _audio_duration_sec(wav_bytes: bytes):
    try:
        with wave.open(io.BytesIO(wav_bytes)) as wf:
            fr = wf.getframerate() or 16000
            return wf.getnframes() / float(fr)
    except Exception:
        return None


def _fmt_usd(x):
    if x is None:
        return "н/д"
    return f"${x:.5f}  (~{x * USD_TO_KZT:.2f} ₸)"


async def run(path: str, glossary: list[str] | None, threshold_override):
    if not os.path.exists(path):
        print(_r(f"Файл не найден: {path}"))
        return 1

    wav = open(path, "rb").read()
    size_kb = len(wav) // 1024
    dur = _audio_duration_sec(wav)

    # Импортируем ПОСЛЕ парса аргументов, чтобы --help работал без env
    from backend.config import settings
    from backend.services import audio_analyzer as A

    if not settings.OPENAI_API_KEY:
        print(_r("OPENAI_API_KEY не задан — STT и реконструкция работать не будут."))
        return 1

    threshold = threshold_override if threshold_override is not None else settings.RMS_SILENCE_THRESHOLD

    print()
    print(_b("═" * 60))
    print(_b(f"  АНАЛИЗ: {os.path.basename(path)}"))
    print(_b("═" * 60))
    print(f"  Размер:        {size_kb} КБ")
    print(f"  Длительность:  {f'{dur:.1f} сек' if dur else 'н/д (не WAV?)'}")
    if glossary:
        print(f"  Глоссарий:     {', '.join(glossary[:12])}{'…' if len(glossary) > 12 else ''}")
    print()

    total_cost = 0.0
    total_time = 0.0

    # ── ШАГ 1: RMS-фильтр ────────────────────────────────────
    t0 = time.perf_counter()
    rms = A._compute_rms(wav)
    rms_dt = time.perf_counter() - t0
    total_time += rms_dt

    print(_b("ШАГ 1 — RMS-фильтр тишины"))
    print(f"  RMS:    {rms:.0f}   |   порог: {threshold}")
    print(f"  Время:  {rms_dt * 1000:.1f} мс   |   Стоимость: $0 (локально)")

    if threshold > 0 and rms < threshold:
        print(_y(f"  ⇒ ОТФИЛЬТРОВАНО (тишина) — API НЕ вызывается, деньги сэкономлены"))
        print()
        print(_b("ИТОГО:"), f"время {total_time * 1000:.1f} мс, стоимость $0")
        return 0
    print(_g(f"  ⇒ ПРОШЁЛ — есть речь, продолжаем"))
    print()

    # Перехватываем usage gpt-4o-mini для подсчёта стоимости реконструкции
    usage_acc = {"in": 0, "out": 0}
    _orig = A.client.chat.completions.create

    async def _wrapped(**k):
        resp = await _orig(**k)
        u = getattr(resp, "usage", None)
        if u:
            usage_acc["in"]  += getattr(u, "prompt_tokens", 0) or 0
            usage_acc["out"] += getattr(u, "completion_tokens", 0) or 0
        return resp
    A.client.chat.completions.create = _wrapped

    try:
        # ── ШАГ 2: Транскрипция (STT) ────────────────────────
        t0 = time.perf_counter()
        raw = await A._transcribe_audio(wav, model=A._PRIMARY_STT_MODEL, location_glossary=glossary)
        stt_dt = time.perf_counter() - t0
        total_time += stt_dt
        stt_cost = (dur / 60.0 * RATE_STT_PER_MIN) if dur else None
        if stt_cost is not None:
            total_cost += stt_cost

        print(_b(f"ШАГ 2 — Транскрипция ({A._PRIMARY_STT_MODEL})"))
        print(f"  Сырой текст: {_dim(repr(raw)[:300])}")
        print(f"  Время:  {stt_dt:.2f} сек   |   Стоимость: {_fmt_usd(stt_cost)}")
        print()

        if not raw or len(raw.split()) < 2:
            print(_y("  ⇒ STT не дал содержательного текста — реконструкция пропущена"))
            print()
            print(_b("ИТОГО:"), f"время {total_time:.2f} сек, стоимость {_fmt_usd(total_cost)}")
            return 0

        # ── ШАГ 3: Реконструкция (gpt-4o-mini) ───────────────
        usage_acc["in"] = usage_acc["out"] = 0
        t0 = time.perf_counter()
        recon = await A.reconstruct_transcript(raw, None, glossary)
        rec_dt = time.perf_counter() - t0
        total_time += rec_dt
        rec_cost = usage_acc["in"] * RATE_MINI_IN_1M / 1e6 + usage_acc["out"] * RATE_MINI_OUT_1M / 1e6
        total_cost += rec_cost

        conf = recon["confidence"]
        conf_str = f"{conf:.2f}" if conf is not None else "н/д (сбой API, оставлен сырой текст)"
        review = recon["needs_review"]

        print(_b("ШАГ 3 — Реконструкция (gpt-4o-mini)"))
        print(f"  Чистый текст: {_g(repr(recon['text'])[:300])}")
        if recon["corrections"]:
            print(f"  Правки:")
            for c in recon["corrections"]:
                print(f"     {_r(str(c.get('from','')))}  →  {_g(str(c.get('to','')))}")
        else:
            print(f"  Правки:  нет")
        print(f"  Уверенность: {conf_str}")
        if review:
            print(_y(f"  ⚠ ПОМЕТКА: нужна ручная проверка (низкая уверенность)"))
        print(f"  Токены: вход {usage_acc['in']}, выход {usage_acc['out']}")
        print(f"  Время:  {rec_dt:.2f} сек   |   Стоимость: {_fmt_usd(rec_cost)}")
        print()

    finally:
        A.client.chat.completions.create = _orig

    # ── ИТОГО ────────────────────────────────────────────────
    print(_b("═" * 60))
    print(_b("  ИТОГО"))
    print(_b("═" * 60))
    print(f"  Общее время:      {total_time:.2f} сек")
    print(f"  Общая стоимость:  {_fmt_usd(total_cost)}")
    if total_cost:
        per_1000 = total_cost * 1000
        print(_dim(f"  (≈ {_fmt_usd(per_1000)} за 1000 таких записей)"))
    print()
    return 0


def main():
    ap = argparse.ArgumentParser(
        description="Прогон одного аудиофайла через реальный STT-пайплайн (RMS → STT → реконструкция).",
    )
    ap.add_argument("audio", help="путь к аудиофайлу (WAV/MP3/OGG)")
    ap.add_argument("--glossary", default="",
                    help="слова точки через запятую (меню, имена) — как в боевом глоссарии")
    ap.add_argument("--threshold", type=int, default=None,
                    help="переопределить RMS-порог тишины (по умолчанию из настроек)")
    args = ap.parse_args()

    glossary = [w.strip() for w in args.glossary.split(",") if w.strip()] or None
    rc = asyncio.run(run(args.audio, glossary, args.threshold))
    sys.exit(rc or 0)


if __name__ == "__main__":
    main()
