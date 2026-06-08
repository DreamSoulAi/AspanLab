"""
compare_stt.py — сравнение движков распознавания на реальных записях кассы.

Прогоняет КАЖДЫЙ .wav в папке со скриптом через:
  • ISSAI (твой VPS-воркер)        — казахская self-hosted модель
  • gpt-4o-mini-transcribe (OpenAI) — новая первичка
  • whisper-1 (OpenAI)             — старый фолбэк
и кладёт результаты рядом, чтобы видно было кто лучше на ТВОЕЙ речи.

У ISSAI дополнительно показывает segments/elapsed — сразу видно ожил ли VAD
после фикса нормализации (раньше было segments=0 → пустота → IGNORE).

──────────────────────────────────────────────────────────────────────
ЗАПУСК (Windows, в папке с wav-файлами):

  1) положи этот скрипт рядом с .wav файлами
  2) set OPENAI_API_KEY=sk-...
  3) set ISSAI_API_KEY=<ключ воркера>      (если у воркера есть ключ; иначе пропусти)
  4) pip install openai httpx
  5) python compare_stt.py

Результат: печатается в консоль + сохраняется в compare_result.md
──────────────────────────────────────────────────────────────────────
"""

import os
import glob
import time

import httpx
from openai import OpenAI

# ── Настройки (можно править прямо здесь или через переменные окружения) ──
ISSAI_URL = os.getenv("ISSAI_WORKER_URL", "http://213.155.21.25:8010")
ISSAI_KEY = os.getenv("ISSAI_API_KEY", "")     # пусто = без авторизации
OPENAI_MODELS = ["gpt-4o-mini-transcribe", "whisper-1"]   # можно добавить "gpt-4o-transcribe"
LANGUAGE = None    # None = авто-детект (лучше для смешанной рус/каз речи)

# Подсказка распознавателю: типичная касса в Казахстане (рус+каз вперемешку).
PROMPT = (
    "Запись разговора с кассы в Казахстане. Речь смешанная: русский и казахский "
    "вперемешку. Часто звучит оплата: QR, Kaspi QR, каспи, картой, наличными, "
    "терминал, сдача. Сохраняй язык каждой фразы, не переводи, не приукрашивай."
)

client = OpenAI()   # ключ из OPENAI_API_KEY


def via_issai(path: str) -> str:
    """Прогон через VPS-воркер ISSAI. Возвращает строку для отчёта."""
    headers = {"X-API-Key": ISSAI_KEY} if ISSAI_KEY else {}
    t0 = time.time()
    try:
        with open(path, "rb") as f:
            r = httpx.post(
                f"{ISSAI_URL.rstrip('/')}/transcribe",
                files={"audio": ("audio.wav", f, "audio/wav")},
                data={"language": "auto"},
                headers=headers,
                timeout=300.0,   # CPU-инференция медленная
            )
        dt = time.time() - t0
        if r.status_code != 200:
            return f"HTTP {r.status_code}: {r.text[:160]}"
        d = r.json()
        text = (d.get("text") or "").strip()
        meta = (f"segments={d.get('segments')}, audio={d.get('audio_duration')}с, "
                f"lang={d.get('language')}, {dt:.1f}с")
        return f"[{meta}]\n    {text or '(ПУСТО — IGNORE)'}"
    except Exception as e:
        return f"ОШИБКА связи с воркером: {type(e).__name__}: {str(e)[:160]}"


def via_openai(path: str, model: str) -> str:
    """Прогон через OpenAI-модель."""
    t0 = time.time()
    try:
        kwargs = dict(model=model, file=open(path, "rb"),
                      response_format="text", prompt=PROMPT)
        if LANGUAGE:
            kwargs["language"] = LANGUAGE
        text = str(client.audio.transcriptions.create(**kwargs)).strip()
        return f"[{time.time()-t0:.1f}с]\n    {text or '(пусто)'}"
    except Exception as e:
        return f"ОШИБКА: {type(e).__name__}: {str(e)[:160]}"


def main():
    files = sorted(glob.glob("*.wav") + glob.glob("*.mp3") + glob.glob("*.m4a"))
    if not files:
        print("Не нашёл аудиофайлов (.wav/.mp3/.m4a) в этой папке.")
        print("Положи записи рядом со скриптом и запусти снова.")
        return

    print(f"Нашёл файлов: {len(files)}")
    print(f"ISSAI: {ISSAI_URL}  (ключ: {'есть' if ISSAI_KEY else 'нет'})")
    print(f"OpenAI модели: {', '.join(OPENAI_MODELS)}\n")

    out = ["# Сравнение STT-движков на записях кассы\n",
           f"ISSAI: `{ISSAI_URL}` · OpenAI: {', '.join(OPENAI_MODELS)}\n"]

    for path in files:
        print(f"\n{'='*60}\n=== {path} ===")
        out.append(f"\n## {path}\n")

        print("  [ISSAI] думаю (CPU, может быть долго)...")
        issai_res = via_issai(path)
        print(f"  [ISSAI]\n  {issai_res}")
        out.append(f"### ISSAI (VPS)\n```\n{issai_res}\n```\n")

        for model in OPENAI_MODELS:
            res = via_openai(path, model)
            print(f"  [{model}]\n  {res}")
            out.append(f"### {model}\n```\n{res}\n```\n")

    with open("compare_result.md", "w", encoding="utf-8") as fp:
        fp.write("\n".join(out))
    print(f"\n{'='*60}\n→ Всё сохранено в compare_result.md")


if __name__ == "__main__":
    main()
