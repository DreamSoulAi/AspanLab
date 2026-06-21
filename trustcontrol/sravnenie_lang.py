#!/usr/bin/env python3
# ════════════════════════════════════════════════════════════
#  sravnenie_lang.py — ПАРТНЁР ДВАЖДЫ (language=kk + language=ru) + СКЛЕЙКА.
#
#  Идея партнёра: вместо одного «auto»-вызова объединённой модели сделать
#  ДВА явных прогона на ТОЙ ЖЕ модели — один с language=kk, другой с
#  language=ru — и наложить их склейкой (как наша гибридная merge ISSAI+OpenAI).
#  Тогда казахские слова берутся из kk-прогона, русские — из ru-прогона,
#  суммы/оплата не теряются.
#
#  Для КАЖДОГО файла секция с тремя блоками:
#    (kk) партнёр language=kk
#    (ru) партнёр language=ru
#    (с)  склейка kk+ru  (gpt-4o-mini, тот же merge что в проде; А=kk, Б=ru)
#
#  ⚠️ Склейка (с) требует баланса OpenAI (gpt-4o-mini). Если баланса нет —
#     блок (с) пометится «недоступна», а (kk)/(ru) всё равно покажутся
#     (партнёрская модель денег не стоит).
#
#  Запуск (внутри контейнера api):
#    python sravnenie_lang.py /app/pw --out /app/results_lang.md
#
#  Забрать на хост:
#    docker compose -f docker-compose.prod.yml --env-file .env.prod \
#        cp api:/app/results_lang.md ./results_lang.md
# ════════════════════════════════════════════════════════════

import argparse
import asyncio
import glob
import io
import os
import sys
import time
import wave

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sravnenie as S


def _audio_duration_sec(wav_bytes: bytes):
    try:
        with wave.open(io.BytesIO(wav_bytes)) as wf:
            fr = wf.getframerate() or 16000
            return wf.getnframes() / float(fr)
    except Exception:
        return None


async def _partner_lang(wav_bytes: bytes, lang: str) -> str:
    """Партнёрская модель с ЯВНЫМ language (kk/ru) — не auto."""
    from openai import AsyncOpenAI
    cli = AsyncOpenAI(api_key=S.PARTNER_TOKEN or "not-needed", base_url=S.PARTNER_URL,
                      timeout=S.PARTNER_TIMEOUT, max_retries=0)
    buf = io.BytesIO(wav_bytes)
    buf.name = "audio.wav"
    kwargs = {"model": S.PARTNER_MODEL, "file": buf, "language": lang}
    if S.PARTNER_VAD:
        kwargs["extra_body"] = {"vad_filter": True}
    tr = await cli.audio.transcriptions.create(**kwargs)
    return (getattr(tr, "text", "") or "").strip()


async def timed(coro):
    t0 = time.perf_counter()
    try:
        res = await coro
        return {"ok": True, "val": res, "dt": time.perf_counter() - t0}
    except Exception as e:
        return {"ok": False, "err": f"{type(e).__name__}: {str(e)[:200]}",
                "dt": time.perf_counter() - t0}


async def _one_file(path: str, A):
    wav = open(path, "rb").read()
    size_kb = len(wav) // 1024
    dur = _audio_duration_sec(wav)

    # Два явных прогона партнёра параллельно
    kk_res, ru_res = await asyncio.gather(
        timed(_partner_lang(wav, "kk")),
        timed(_partner_lang(wav, "ru")),
    )
    kk_text = kk_res["val"] if kk_res["ok"] else ""
    ru_text = ru_res["val"] if ru_res["ok"] else ""

    # Склейка kk(А) + ru(Б) — тот же прод-merge. Best-effort (нужен OpenAI).
    t0 = time.perf_counter()
    try:
        merged = await A._merge_transcripts(kk_text, ru_text, None)
        c_res = {"ok": True, "val": merged.get("text", ""),
                 "dt": time.perf_counter() - t0, "conf": merged.get("confidence")}
    except Exception as e:
        c_res = {"ok": False, "err": f"{type(e).__name__}: {str(e)[:200]}",
                 "dt": time.perf_counter() - t0}

    return {"name": os.path.basename(path), "size_kb": size_kb, "dur": dur,
            "kk": kk_res, "ru": ru_res, "c": c_res}


def _md_block(tag, title, res, conf=None):
    out = [f"### ({tag}) {title}", ""]
    if res["ok"]:
        txt = (res["val"] or "").strip()
        words = len(txt.split())
        out += ["```", txt if txt else "(пусто)", "```"]
        meta = f"_слов: {words} · время: {res['dt']:.2f}с"
        if conf is not None:
            meta += f" · уверенность склейки: {conf:.2f}"
        meta += "_"
        out.append(meta)
    else:
        out.append(f"> ⊘ ПРОПУЩЕН: {res['err']}  _(время до ошибки {res['dt']:.2f}с)_")
    out.append("")
    return out


async def run(folder: str, out_path: str) -> int:
    files = sorted(glob.glob(os.path.join(folder, "*.wav")))
    if not files:
        print(S._r(f"В папке нет .wav: {folder}"))
        return 1

    from backend.services import audio_analyzer as A

    print(S._b(f"Прогон language kk+ru: {len(files)} файлов из {folder}"))
    print(S._dim(f"партнёр: {S.PARTNER_MODEL} @ {S.PARTNER_URL}  vad={'on' if S.PARTNER_VAD else 'off'}"))
    print()

    md = [
        "# Партнёр: два прогона (language=kk + language=ru) + склейка",
        "",
        f"Папка: `{folder}` · файлов: **{len(files)}**",
        f"Партнёр: `{S.PARTNER_MODEL}` @ `{S.PARTNER_URL}` · vad_filter={'on' if S.PARTNER_VAD else 'off'}",
        "",
        "Источники: **(kk)** партнёр language=kk · **(ru)** партнёр language=ru · "
        "**(с)** склейка kk+ru (gpt-4o-mini, А=kk Б=ru). "
        "Склейка требует баланса OpenAI — без него пометится «пропущена».",
        "",
        "---",
        "",
    ]

    n_kk = n_ru = n_c = 0
    for i, path in enumerate(files, 1):
        print(f"  [{i}/{len(files)}] {os.path.basename(path)} …", flush=True)
        r = await _one_file(path, A)
        if r["kk"]["ok"]: n_kk += 1
        if r["ru"]["ok"]: n_ru += 1
        if r["c"]["ok"]:  n_c += 1

        dur_s = f"{r['dur']:.0f}с" if r["dur"] else "н/д"
        md += [f"## {i}. {r['name']}", "", f"Размер {r['size_kb']} КБ · длительность {dur_s}", ""]
        md += _md_block("kk", "партнёр language=kk", r["kk"])
        md += _md_block("ru", "партнёр language=ru", r["ru"])
        md += _md_block("с", "склейка kk+ru (А=kk, Б=ru)", r["c"],
                        r["c"].get("conf") if r["c"]["ok"] else None)
        md += ["---", ""]

    md += [
        "## Итог",
        "",
        f"- (kk) партнёр language=kk: успешно **{n_kk}/{len(files)}**",
        f"- (ru) партнёр language=ru: успешно **{n_ru}/{len(files)}**",
        f"- (с)  склейка kk+ru: успешно **{n_c}/{len(files)}**",
        "",
        "**Что смотреть:** даёт ли явный kk меньше галлюцинаций на казахском · "
        "явный ru чище на русском · склейка собирает оба без потери сумм.",
        "",
    ]

    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(md))

    print()
    print(S._g(f"✓ Готово: {out_path}"))
    print(S._dim(f"  (kk) {n_kk}/{len(files)} · (ru) {n_ru}/{len(files)} · (с) {n_c}/{len(files)}"))
    return 0


def main():
    ap = argparse.ArgumentParser(description="Партнёр kk+ru + склейка по папке WAV.")
    ap.add_argument("folder", nargs="?", default="/app/pw", help="папка с WAV (дефолт /app/pw)")
    ap.add_argument("--out", default="/app/results_lang.md", help="куда писать markdown")
    args = ap.parse_args()
    sys.exit(asyncio.run(run(args.folder, args.out)) or 0)


if __name__ == "__main__":
    main()
