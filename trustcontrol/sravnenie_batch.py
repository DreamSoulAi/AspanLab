#!/usr/bin/env python3
# ════════════════════════════════════════════════════════════
#  sravnenie_batch.py — ПАКЕТНОЕ сравнение STT на ПАПКЕ WAV-файлов.
#
#  То же что sravnenie.py, но прогоняет сразу всю папку и пишет
#  результат в один results.md (Markdown), удобно сравнивать пачку.
#
#  Для КАЖДОГО файла = отдельная секция с тремя транскриптами:
#    (а) gpt-4o-transcribe   — наш текущий основной путь (OpenAI)
#    (в) финальная склейка   — что реально идёт в отчёт (merge ISSAI+OpenAI)
#    (п) партнёр объединённая — beketkz/whisper-kaz-rus-ct2 (Bearer + vad_filter)
#
#  Один файл = одна запись = один транскрипт (нарезку на клиентов тут не делаем,
#  это сравнение СЫРОГО распознавания; нарезка живёт в проде dialog_splitter).
#
#  Запуск (внутри контейнера api):
#    python sravnenie_batch.py                 # папка /app/pw по умолчанию
#    python sravnenie_batch.py /app/pw
#    python sravnenie_batch.py /app/pw --out /app/results.md
#    python sravnenie_batch.py /app/pw --glossary "рожок,коктейль,клубника-банан"
#
#  Забрать results.md на хост:
#    docker compose -f docker-compose.prod.yml --env-file .env.prod \
#        cp api:/app/results.md ./results.md
#
#  Env как у sravnenie.py: OPENAI_API_KEY (а/в), ISSAI_WORKER_URL (в),
#  PARTNER_URL/MODEL/TOKEN/LANG/VAD (п).
# ════════════════════════════════════════════════════════════

import argparse
import asyncio
import glob
import io
import os
import sys
import time
import wave

# Переиспользуем настройки/функцию партнёра из sravnenie.py — единый источник
# истины, чтобы пакетный прогон 1-в-1 совпадал с одиночным.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sravnenie as S


def _audio_duration_sec(wav_bytes: bytes):
    try:
        with wave.open(io.BytesIO(wav_bytes)) as wf:
            fr = wf.getframerate() or 16000
            return wf.getnframes() / float(fr)
    except Exception:
        return None


async def _one_file(path: str, glossary, lang, A, issai_stt):
    """Гонит a/в/п на одном файле, возвращает dict для markdown."""
    wav = open(path, "rb").read()
    size_kb = len(wav) // 1024
    dur = _audio_duration_sec(wav)

    # Перехват usage gpt-4o-mini для стоимости склейки (в)
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

    async def timed(coro):
        t0 = time.perf_counter()
        try:
            res = await coro
            return {"ok": True, "val": res, "dt": time.perf_counter() - t0}
        except Exception as e:
            return {"ok": False, "err": f"{type(e).__name__}: {str(e)[:200]}",
                    "dt": time.perf_counter() - t0}

    src_a = timed(A._transcribe_audio(wav, model=A._PRIMARY_STT_MODEL, location_glossary=glossary))
    if issai_stt.is_enabled():
        src_b = timed(issai_stt.transcribe(wav, lang=lang))
    else:
        async def _disabled():
            raise RuntimeError("ISSAI_WORKER_URL не задан")
        src_b = timed(_disabled())
    src_p = timed(S._partner_transcribe(wav))

    a_res, b_res, p_res = await asyncio.gather(src_a, src_b, src_p)
    a_text = a_res["val"] if a_res["ok"] else ""
    b_text = b_res["val"] if b_res["ok"] else ""

    # (в) склейка — ровно как в проде
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

    cost_a = (dur / 60.0 * S.RATE_STT_PER_MIN) if dur else None
    cost_c = (cost_a or 0) + usage_acc["in"] * S.RATE_MINI_IN_1M / 1e6 + usage_acc["out"] * S.RATE_MINI_OUT_1M / 1e6

    return {"name": os.path.basename(path), "size_kb": size_kb, "dur": dur,
            "a": a_res, "c": c_res, "p": p_res, "cost_a": cost_a, "cost_c": cost_c}


def _md_block(tag, title, res, cost=None, conf=None):
    out = [f"### ({tag}) {title}", ""]
    if res["ok"]:
        txt = (res["val"] or "").strip()
        words = len(txt.split())
        out.append("```")
        out.append(txt if txt else "(пусто)")
        out.append("```")
        meta = f"_слов: {words} · время: {res['dt']:.2f}с"
        if cost is not None:
            meta += f" · стоимость: {S._fmt_usd(cost)}"
        if conf is not None:
            meta += f" · уверенность склейки: {conf:.2f}"
        meta += "_"
        out.append(meta)
    else:
        out.append(f"> ⊘ ПРОПУЩЕН: {res['err']}  _(время до ошибки {res['dt']:.2f}с)_")
    out.append("")
    return out


async def run(folder: str, out_path: str, glossary, lang) -> int:
    files = sorted(glob.glob(os.path.join(folder, "*.wav")))
    if not files:
        print(S._r(f"В папке нет .wav: {folder}"))
        return 1

    from backend.services import audio_analyzer as A
    from backend.services import issai_stt

    print(S._b(f"Пакетный прогон: {len(files)} файлов из {folder}"))
    print(S._dim(f"партнёр: {S.PARTNER_MODEL} @ {S.PARTNER_URL}  vad={'on' if S.PARTNER_VAD else 'off'}"))
    print()

    md = [
        "# Сравнение STT — пакетный прогон",
        "",
        f"Папка: `{folder}` · файлов: **{len(files)}**",
        f"Партнёр: `{S.PARTNER_MODEL}` @ `{S.PARTNER_URL}` · vad_filter={'on' if S.PARTNER_VAD else 'off'}",
        "",
        "Источники: **(а)** gpt-4o-transcribe · **(в)** наша склейка (идёт в отчёт) · "
        "**(п)** партнёр объединённая модель.",
        "",
        "---",
        "",
    ]

    n_a = n_c = n_p = 0
    for i, path in enumerate(files, 1):
        print(f"  [{i}/{len(files)}] {os.path.basename(path)} …", flush=True)
        r = await _one_file(path, glossary, lang, A, issai_stt)
        if r["a"]["ok"]: n_a += 1
        if r["c"]["ok"]: n_c += 1
        if r["p"]["ok"]: n_p += 1

        dur_s = f"{r['dur']:.0f}с" if r["dur"] else "н/д"
        md += [
            f"## {i}. {r['name']}",
            "",
            f"Размер {r['size_kb']} КБ · длительность {dur_s}",
            "",
        ]
        md += _md_block("а", "gpt-4o-transcribe", r["a"], r["cost_a"])
        md += _md_block("в", "финальная склейка (в отчёт)", r["c"],
                        r["cost_c"] if r["c"]["ok"] else None,
                        r["c"].get("conf") if r["c"]["ok"] else None)
        md += _md_block("п", f"партнёр ({S.PARTNER_MODEL})", r["p"], 0.0)
        md += ["---", ""]

    md += [
        "## Итог",
        "",
        f"- (а) gpt-4o-transcribe: успешно **{n_a}/{len(files)}**",
        f"- (в) склейка: успешно **{n_c}/{len(files)}**",
        f"- (п) партнёр: успешно **{n_p}/{len(files)}**",
        "",
        "**Что смотреть глазами:** казахский без галлюцинаций · русский не хуже OpenAI · "
        "переключение языков (шала-каз) в одной фразе · точность цифр/сумм (критично для фрода).",
        "",
    ]

    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(md))

    print()
    print(S._g(f"✓ Готово: {out_path}"))
    print(S._dim(f"  (а) {n_a}/{len(files)} · (в) {n_c}/{len(files)} · (п) {n_p}/{len(files)}"))
    print()
    print(S._dim("Забрать на хост:"))
    print(S._dim("  docker compose -f docker-compose.prod.yml --env-file .env.prod \\"))
    print(S._dim(f"      cp api:{out_path} ./results.md"))
    return 0


def main():
    ap = argparse.ArgumentParser(description="Пакетное сравнение STT (а/в/п) по папке WAV.")
    ap.add_argument("folder", nargs="?", default="/app/pw", help="папка с WAV (дефолт /app/pw)")
    ap.add_argument("--out", default="/app/results.md", help="куда писать markdown (дефолт /app/results.md)")
    ap.add_argument("--glossary", default="", help="слова точки через запятую")
    ap.add_argument("--lang", default=None, help="код языка для ISSAI (kk/ru/auto)")
    args = ap.parse_args()
    glossary = [w.strip() for w in args.glossary.split(",") if w.strip()] or None
    sys.exit(asyncio.run(run(args.folder, args.out, glossary, args.lang)) or 0)


if __name__ == "__main__":
    main()
