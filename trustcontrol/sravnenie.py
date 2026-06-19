#!/usr/bin/env python3
# ════════════════════════════════════════════════════════════
#  sravnenie.py — СЛЕПОЕ СРАВНЕНИЕ 5 источников распознавания
#  на ОДНОМ WAV-файле. Прогоняет параллельно и печатает
#  транскрипты РЯДОМ, чтобы оценить качество глазами.
#
#  Источники:
#    (а) gpt-4o-transcribe        — наш текущий основной путь (платный OpenAI)
#    (б) ISSAI                     — наш казахский self-hosted воркер (VPS)
#    (в) ФИНАЛЬНАЯ СКЛЕЙКА         — что РЕАЛЬНО идёт в отчёт сейчас
#                                    (merge ISSAI+OpenAI через gpt-4o-mini)
#    (г) партнёр КАЗАХСКАЯ модель  — GPU-сервер партнёра, порт 8000
#                                    (shyngys879/kazakh-whisper-large-v3-turbo, kk)
#    (д) партнёр РУССКАЯ модель    — GPU-сервер партнёра, порт 8001
#                                    (coriollon/whisper-large-v3-turbo-russian, ru)
#
#  Для каждого: транскрипт, время обработки, примерная стоимость.
#  Если endpoint недоступен — помечается ПРОПУЩЕН, остальные считаются
#  (один упавший источник НЕ роняет весь скрипт).
#
#  ЭТО ТОЛЬКО ТЕСТ СРАВНЕНИЯ. В прод НЕ встроено, пайплайн не трогает.
#
#  Запуск:
#    python sravnenie.py запись.wav
#    python sravnenie.py запись.wav --glossary "капучино,рожок,Айгуль"
#    python sravnenie.py --probe          (проверить ОБА сервера партнёра)
#
#  Серверы партнёра настраиваются через env (или дефолты из задачи):
#    PARTNER_KK_URL / PARTNER_KK_MODEL / PARTNER_KK_LANG   (порт 8000, kk)
#    PARTNER_RU_URL / PARTNER_RU_MODEL / PARTNER_RU_LANG   (порт 8001, ru)
#
#  Требует env: OPENAI_API_KEY (для а/в), ISSAI_WORKER_URL (для б/в).
#  Использует ТЕ ЖЕ функции что боевой сервер (audio_analyzer/issai_stt),
#  поэтому (а)/(б)/(в) отражают реальный прод, а не пересказ логики.
# ════════════════════════════════════════════════════════════

import argparse
import asyncio
import io
import os
import sys
import time
import wave

# ── Тарифы (USD). Переопределяются env если цены изменятся ──
RATE_STT_PER_MIN = float(os.getenv("RATE_STT_PER_MIN", "0.006"))       # gpt-4o-transcribe
RATE_MINI_IN_1M  = float(os.getenv("RATE_MINI_IN_PER_1M", "0.15"))     # gpt-4o-mini вход
RATE_MINI_OUT_1M = float(os.getenv("RATE_MINI_OUT_PER_1M", "0.60"))    # gpt-4o-mini выход
USD_TO_KZT       = float(os.getenv("USD_TO_KZT", "470"))

# ── Серверы партнёра (две модели на GPU, OpenAI-совместимые) ──
#   (г) казахская — порт 8000
PARTNER_KK_URL   = os.getenv("PARTNER_KK_URL",   "http://2.133.48.5.dynamic.telecom.kz:8000/v1")
PARTNER_KK_MODEL = os.getenv("PARTNER_KK_MODEL", "shyngys879/kazakh-whisper-large-v3-turbo")
PARTNER_KK_LANG  = os.getenv("PARTNER_KK_LANG",  "kk")
#   (д) русская — порт 8001
PARTNER_RU_URL   = os.getenv("PARTNER_RU_URL",   "http://2.133.48.5.dynamic.telecom.kz:8001/v1")
PARTNER_RU_MODEL = os.getenv("PARTNER_RU_MODEL", "coriollon/whisper-large-v3-turbo-russian")
PARTNER_RU_LANG  = os.getenv("PARTNER_RU_LANG",  "ru")

PARTNER_TIMEOUT = float(os.getenv("PARTNER_STT_TIMEOUT", "120"))

# ── Цвета (отключаются если не tty) ──
_C = sys.stdout.isatty()
def _b(s):   return f"\033[1m{s}\033[0m"  if _C else s
def _g(s):   return f"\033[32m{s}\033[0m" if _C else s
def _y(s):   return f"\033[33m{s}\033[0m" if _C else s
def _r(s):   return f"\033[31m{s}\033[0m" if _C else s
def _dim(s): return f"\033[2m{s}\033[0m"  if _C else s


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
    if x == 0:
        return "$0 (бесплатно для нас)"
    return f"${x:.5f}  (~{x * USD_TO_KZT:.3f} ₸)"


# ════════════════════════════════════════════════════════════
#  Сервер партнёра — через ОФИЦИАЛЬНЫЙ OpenAI SDK с base_url.
#  Если это сработает — endpoint доказанно OpenAI-совместим:
#  слать можно ровно так же просто, как в настоящий OpenAI.
#  Параметризован (url/model/lang) — одна функция на оба порта.
# ════════════════════════════════════════════════════════════
async def _partner_transcribe(wav_bytes: bytes, url: str, model: str, lang: str | None) -> str:
    from openai import AsyncOpenAI
    cli = AsyncOpenAI(api_key="not-needed", base_url=url, timeout=PARTNER_TIMEOUT, max_retries=0)
    buf = io.BytesIO(wav_bytes)
    buf.name = "audio.wav"
    kwargs = {"model": model, "file": buf}
    # language НЕ обязателен (модель сама определяет), но OpenAI-совместимый
    # сервер обязан его принять. Передаём явно — это заодно проверка совместимости.
    if lang:
        kwargs["language"] = lang
    tr = await cli.audio.transcriptions.create(**kwargs)
    return (getattr(tr, "text", "") or "").strip()


async def _probe_one(label: str, base: str, model: str) -> bool:
    """Проверка одного сервера: достижимость + OpenAI-совместимость (/v1/models)."""
    import httpx
    base = base.rstrip("/")
    print(_b(f"  [{label}]"))
    print(f"     URL:    {base}")
    print(f"     модель: {model}")
    models_url = f"{base}/models"
    print(_dim(f"     GET {models_url} …"))
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(models_url)
        print(_g(f"     ✓ сервер ответил: HTTP {r.status_code}"))
        body = r.text[:300]
        if '"data"' in body or '"object"' in body:
            print(_g("     ✓ формат ответа похож на OpenAI (/v1/models)"))
        else:
            print(_y(f"     ~ ответ есть, но не похож на OpenAI /v1/models: {body[:120]}"))
        return True
    except Exception as e:
        print(_r(f"     ✗ недоступен: {type(e).__name__}: {str(e)[:160]}"))
        return False


async def _probe_partner() -> int:
    """Техническая проверка: живы ли ОБА сервера партнёра и OpenAI-совместимы ли."""
    print(_b("ПРОВЕРКА СЕРВЕРОВ ПАРТНЁРА (два порта)"))
    print()
    ok_kk = await _probe_one("г · казахская · порт 8000", PARTNER_KK_URL, PARTNER_KK_MODEL)
    print()
    ok_ru = await _probe_one("д · русская · порт 8001", PARTNER_RU_URL, PARTNER_RU_MODEL)
    print()
    if ok_kk and ok_ru:
        print(_g("  ✓ Оба сервера доступны — можно гонять сравнение."))
    elif ok_kk or ok_ru:
        print(_y("  ~ Доступен только ОДИН сервер. Сравнение пройдёт, недоступный помечу ПРОПУЩЕН."))
    else:
        print(_r("  ✗ Оба недоступны: GPU выключен / сменился динамический IP /"))
        print(_r("    порт закрыт / нет исходящей сети из этой среды."))
    print()
    print(_dim("  Полный прогон: python sravnenie.py запись.wav"))
    return 0 if (ok_kk or ok_ru) else 1


# ════════════════════════════════════════════════════════════
#  Основной прогон сравнения
# ════════════════════════════════════════════════════════════
async def run(path: str, glossary: list[str] | None, lang: str | None) -> int:
    if not os.path.exists(path):
        print(_r(f"Файл не найден: {path}"))
        return 1

    wav = open(path, "rb").read()
    size_kb = len(wav) // 1024
    dur = _audio_duration_sec(wav)

    from backend.config import settings
    from backend.services import audio_analyzer as A
    from backend.services import issai_stt

    print()
    print(_b("═" * 64))
    print(_b(f"  СРАВНЕНИЕ РАСПОЗНАВАНИЯ: {os.path.basename(path)}"))
    print(_b("═" * 64))
    print(f"  Размер:        {size_kb} КБ")
    print(f"  Длительность:  {f'{dur:.1f} сек' if dur else 'н/д (не WAV?)'}")
    if glossary:
        print(f"  Глоссарий:     {', '.join(glossary[:12])}{'…' if len(glossary) > 12 else ''}")
    print()

    # ── Перехват usage gpt-4o-mini (для стоимости склейки в) ──
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

    # ── Обёртки с замером времени и аккуратной обработкой ошибок ──
    async def timed(coro):
        t0 = time.perf_counter()
        try:
            res = await coro
            return {"ok": True, "val": res, "dt": time.perf_counter() - t0}
        except Exception as e:
            return {"ok": False, "err": f"{type(e).__name__}: {str(e)[:200]}",
                    "dt": time.perf_counter() - t0}

    # (а) gpt-4o-transcribe — наш основной путь
    src_a = timed(A._transcribe_audio(wav, model=A._PRIMARY_STT_MODEL, location_glossary=glossary))

    # (б) ISSAI — наш казахский воркер (lang как в проде: auto, если не задан явно)
    if issai_stt.is_enabled():
        src_b = timed(issai_stt.transcribe(wav, lang=lang))
    else:
        async def _disabled():
            raise RuntimeError("ISSAI_WORKER_URL не задан — воркер выключен")
        src_b = timed(_disabled())

    # (г) партнёр КАЗАХСКАЯ модель — порт 8000
    src_g = timed(_partner_transcribe(wav, PARTNER_KK_URL, PARTNER_KK_MODEL, PARTNER_KK_LANG))

    # (д) партнёр РУССКАЯ модель — порт 8001
    src_d = timed(_partner_transcribe(wav, PARTNER_RU_URL, PARTNER_RU_MODEL, PARTNER_RU_LANG))

    # Запускаем а, б, г, д ПАРАЛЛЕЛЬНО. (в) считается из а+б после.
    a_res, b_res, g_res, d_res = await asyncio.gather(src_a, src_b, src_g, src_d)

    a_text = a_res["val"] if a_res["ok"] else ""
    b_text = b_res["val"] if b_res["ok"] else ""

    # (в) ФИНАЛЬНАЯ СКЛЕЙКА — ровно как в проде: merge ISSAI+OpenAI
    usage_acc["in"] = usage_acc["out"] = 0
    t0 = time.perf_counter()
    try:
        merged = await A._merge_transcripts(b_text, a_text, None)
        c_res = {"ok": True, "val": merged.get("text", ""), "dt": time.perf_counter() - t0,
                 "conf": merged.get("confidence")}
    except Exception as e:
        c_res = {"ok": False, "err": f"{type(e).__name__}: {str(e)[:200]}",
                 "dt": time.perf_counter() - t0}

    A.client.chat.completions.create = _orig

    # ── Стоимости ──
    cost_a = (dur / 60.0 * RATE_STT_PER_MIN) if dur else None
    cost_b = 0.0  # self-hosted VPS — переменка ≈ 0
    cost_c = (cost_a or 0) + usage_acc["in"] * RATE_MINI_IN_1M / 1e6 + usage_acc["out"] * RATE_MINI_OUT_1M / 1e6
    cost_g = 0.0  # GPU партнёра — для нас бесплатно
    cost_d = 0.0  # GPU партнёра — для нас бесплатно

    # ── Вывод РЯДОМ ──
    def block(tag, title, res, cost, extra=""):
        print(_b("─" * 64))
        print(_b(f"  ({tag}) {title}"))
        print(_b("─" * 64))
        if res["ok"]:
            txt = res["val"] or ""
            words = len(txt.split())
            print(f"  {_g(txt) if txt else _y('(пусто)')}")
            print()
            print(_dim(f"  слов: {words}  |  время: {res['dt']:.2f} сек  |  стоимость: {_fmt_usd(cost)}{extra}"))
        else:
            print(_y(f"  ⊘ ПРОПУЩЕН — недоступен / ошибка: {res['err']}"))
            print(_dim(f"  время до ошибки: {res['dt']:.2f} сек"))
        print()

    print()
    block("а", "gpt-4o-transcribe  (наш текущий основной)", a_res, cost_a)
    block("б", "ISSAI  (наш казахский self-hosted)", b_res, cost_b)
    conf = c_res.get("conf")
    conf_extra = f"  |  уверенность склейки: {conf:.2f}" if c_res["ok"] and conf is not None else ""
    block("в", "ФИНАЛЬНАЯ СКЛЕЙКА  (идёт в отчёт СЕЙЧАС)", c_res, cost_c if c_res["ok"] else None, conf_extra)
    block("г", f"партнёр КАЗАХСКАЯ  (GPU, порт 8000 · {PARTNER_KK_MODEL})", g_res, cost_g)
    block("д", f"партнёр РУССКАЯ  (GPU, порт 8001 · {PARTNER_RU_MODEL})", d_res, cost_d)

    # ── Подсказка по глазам ──
    print(_b("═" * 64))
    print(_b("  ЧТО СМОТРЕТЬ ГЛАЗАМИ"))
    print(_b("═" * 64))
    print("  • Чистый КАЗАХСКИЙ:  кто точнее — (б) ISSAI или (г) партнёр-каз?")
    print("  • Чистый РУССКИЙ:    кто точнее — (а) OpenAI или (д) партнёр-рус?")
    print("  • СМЕСЬ (шала-каз):  держит ли кто-то переключение лучше склейки (в)?")
    print("  • Цифры/номера/суммы — у кого точнее (критично для фрод-детекции).")
    print(_dim(f"  Текущая переменка за запись: склейка (в) = {_fmt_usd(cost_c)};"))
    print(_dim(f"  партнёр (г)/(д) = $0. Если партнёр ≈ склейка по качеству —"))
    print(_dim(f"  он заменяет связку а+б+склейку и убирает платный OpenAI STT."))
    print()
    return 0


def main():
    ap = argparse.ArgumentParser(
        description="Слепое сравнение 5 STT-источников на одном WAV (а/б/в/г/д).",
    )
    ap.add_argument("audio", nargs="?", help="путь к WAV-файлу")
    ap.add_argument("--glossary", default="",
                    help="слова точки через запятую (меню, имена) — как в боевом глоссарии")
    ap.add_argument("--lang", default=None,
                    help="код языка для ISSAI (kk/ru/auto). По умолчанию auto. "
                         "Модели партнёра используют свой язык (г=kk, д=ru).")
    ap.add_argument("--probe", action="store_true",
                    help="только проверить достижимость и OpenAI-совместимость ОБОИХ серверов партнёра")
    args = ap.parse_args()

    if args.probe:
        sys.exit(asyncio.run(_probe_partner()))

    if not args.audio:
        ap.error("укажи путь к WAV-файлу (или --probe для проверки серверов)")

    glossary = [w.strip() for w in args.glossary.split(",") if w.strip()] or None
    sys.exit(asyncio.run(run(args.audio, glossary, args.lang)) or 0)


if __name__ == "__main__":
    main()
