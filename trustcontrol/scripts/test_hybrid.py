"""
test_hybrid.py — проверка ГИБРИДНОГО движка на реальных записях кассы.

В отличие от compare_stt.py (он показывает только сырой вывод каждого STT
по отдельности), этот скрипт прогоняет КАЖДЫЙ .wav через НАСТОЯЩИЙ
продакшн-пайплайн:

    ISSAI ‖ OpenAI  (параллельно)
        → _merge_transcripts  (GPT объединяет лучшее + чинит врачок→рожок, кера→QR)
        → gpt_analyze         (статус OK/PERSONAL/IGNORE, score, события, summary)

То есть видно РОВНО то, что попадёт в отчёт на дашборде, ещё до деплоя.

──────────────────────────────────────────────────────────────────────
ЗАПУСК (в папке trustcontrol/):

  set OPENAI_API_KEY=sk-...
  set ISSAI_WORKER_URL=http://213.155.21.25:8010
  set ISSAI_WORKER_KEY=<ключ воркера, если есть; иначе пропусти>
  pip install -r requirements.txt
  python scripts/test_hybrid.py <папка_или_файл.wav>

Если путь не указан — берёт все .wav/.mp3/.m4a в текущей папке.
Результат печатается в консоль + сохраняется в hybrid_result.md
──────────────────────────────────────────────────────────────────────
"""

import asyncio
import glob
import os
import sys

# Скрипт лежит в trustcontrol/scripts/ — добавляем корень проекта в путь,
# чтобы импортировался пакет backend.* без установки.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.services import audio_analyzer as A
from backend.services import issai_stt
from backend.services.gpt_analyzer import gpt_analyze


def _collect_files(arg: str | None) -> list[str]:
    if arg and os.path.isfile(arg):
        return [arg]
    folder = arg if (arg and os.path.isdir(arg)) else "."
    files: list[str] = []
    for ext in ("*.wav", "*.mp3", "*.m4a"):
        files += glob.glob(os.path.join(folder, ext))
    return sorted(files)


async def run_one(path: str) -> str:
    """Гоняет один файл через гибридный пайплайн, возвращает markdown-блок."""
    with open(path, "rb") as f:
        wav = f.read()

    lines = [f"\n## {os.path.basename(path)}  ({len(wav)//1024} KB)\n"]

    # ── Шаг 1: сырые транскрипты ISSAI и OpenAI (параллельно, как в проде) ──
    issai_diag: dict = {}
    issai_raw, openai_raw = await asyncio.gather(
        issai_stt.transcribe(wav, diag=issai_diag),
        A._transcribe_audio(wav, None, model=A._PRIMARY_STT_MODEL),
    )
    issai_raw  = A._strip_repeat_loops(issai_raw  or "")
    openai_raw = A._strip_repeat_loops(openai_raw or "")

    lines.append(f"**ISSAI (казахский):**\n```\n{issai_raw or '(пусто)'}\n```\n")
    lines.append(f"**OpenAI ({A._PRIMARY_STT_MODEL}):**\n```\n{openai_raw or '(пусто)'}\n```\n")

    # ── Шаг 2: гибридный merge (объединение + починка слов) ──
    merged = await A._merge_transcripts(issai_raw, openai_raw)
    lines.append(f"**🔀 MERGED (что увидит анализ):**\n```\n{merged or '(пусто)'}\n```\n")

    # ── Шаг 3: анализ — статус, балл, события, summary ──
    if merged and len(merged.split()) >= 2:
        gpt = await gpt_analyze(merged)
        if not gpt:
            lines.append("**Анализ:** _GPT не ответил_\n")
        else:
            status = gpt.get("status", "OK")
            ev = gpt.get("events", {}) or {}
            ev_on = ", ".join(k for k, v in ev.items() if v) or "—"
            lines.append(
                "**Анализ:**\n```\n"
                f"status   = {status}\n"
                f"score    = {gpt.get('score')}\n"
                f"tone     = {gpt.get('tone')}\n"
                f"clients  = {gpt.get('customers_served')}\n"
                f"events   = {ev_on}\n"
                f"summary  = {gpt.get('summary', '')}\n"
                "```\n"
            )
    else:
        lines.append("**Анализ:** _пропущен (merged пустой → IGNORE)_\n")

    block = "".join(lines)
    print(block)
    return block


async def main():
    arg = sys.argv[1] if len(sys.argv) > 1 else None
    files = _collect_files(arg)
    if not files:
        print("Не нашёл аудиофайлов (.wav/.mp3/.m4a).")
        print("Укажи путь:  python scripts/test_hybrid.py <папка_или_файл.wav>")
        return

    print(f"Файлов: {len(files)}")
    print(f"ISSAI включён: {issai_stt.is_enabled()}  ({os.getenv('ISSAI_WORKER_URL', '—')})")
    print(f"Первичный STT: {A._PRIMARY_STT_MODEL}\n")

    out = ["# Гибридный движок — проверка на записях кассы\n"]
    for path in files:
        try:
            out.append(await run_one(path))
        except Exception as e:
            err = f"\n## {os.path.basename(path)}\n_ОШИБКА: {type(e).__name__}: {e}_\n"
            print(err)
            out.append(err)

    with open("hybrid_result.md", "w", encoding="utf-8") as fp:
        fp.write("\n".join(out))
    print("\n→ Сохранено в hybrid_result.md")


if __name__ == "__main__":
    asyncio.run(main())
