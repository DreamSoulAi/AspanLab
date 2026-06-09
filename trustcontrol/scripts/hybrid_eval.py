"""
hybrid_eval.py — СВОДКА по записям через НАСТОЯЩИЙ продакшн-пайплайн.

В отличие от hybrid_test.py (показывает сырые транскрипты), этот гоняет каждый
файл через реальный backend (`analyze_audio_with_fallback`) — ровно то, что
попадёт в дашборд — и выдаёт ТАБЛИЦУ: статус, балл, тон, клиентов, события +
авто-флаг «подозрительно» там, где вероятна ошибка. Глянул таблицу + послушал
спорное — вместо чтения простыней.

Работает в двух режимах:
  • ЛОКАЛЬНО (вариант B): ключи на твоём ПК, я их не вижу.
      set OPENAI_API_KEY=sk-...
      set ISSAI_WORKER_URL=http://213.155.21.25:8010
      set ISSAI_WORKER_KEY=...
      python scripts/hybrid_eval.py <папка>
  • В CI (вариант C): ключи в GitHub Secrets, гоняется само (.github/workflows/stt-eval.yml).

Путь не указан → берёт tests/samples/.
Результат: таблица в консоль + hybrid_result.md (+ сводка в GitHub Actions).
"""

import asyncio
import glob
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.services.audio_analyzer import analyze_audio_with_fallback
from backend.services import issai_stt
from backend.services.analyzer import analyze, get_tone, calculate_score


_EVENT_KEYS = ("greeting", "farewell", "upsell", "rudeness", "fraud_attempt", "issue_resolved")


def _collect(arg: str | None) -> list[str]:
    if arg and os.path.isfile(arg):
        return [arg]
    folder = arg if (arg and os.path.isdir(arg)) else "tests/samples"
    files: list[str] = []
    for ext in ("*.wav", "*.mp3", "*.m4a", "*.ogg", "*.webm"):
        files += glob.glob(os.path.join(folder, ext))
    return sorted(files)


def _final_score(result: dict, transcript: str) -> int:
    """Повторяет мостик оценки из api/reports.py — чтобы видеть дашборд-балл."""
    events = result.get("events", {}) or {}
    found = analyze(transcript)
    has_greeting = ("✅ Приветствие" in found) or events.get("greeting")
    has_goodbye  = ("✅ Прощание" in found) or events.get("farewell")
    has_thanks   = "✅ Благодарность" in found
    score_events = dict(events)
    if has_greeting:
        score_events["greeting"] = True
    if has_goodbye or has_thanks:
        score_events["farewell"] = True
    eff_tone = get_tone(result.get("tone", "neutral"), events)
    return calculate_score(
        events=score_events,
        tone=eff_tone,
        fraud_confidence=int(result.get("fraud_confidence", 0) or 0),
        customer_satisfaction=result.get("customer_satisfaction"),
        energy_level=result.get("energy_level"),
    )


def _flags(result: dict, transcript: str, wav_kb: int) -> str:
    """Авто-подсветка вероятных ошибок — на что глянуть глазами/ушами."""
    out = []
    status = result.get("status")
    if status == "IGNORE":
        out.append("⚠ IGNORE — проверь, не живой ли это разговор")
    if status == "PERSONAL":
        out.append("PERSONAL — точно не было заказа?")
    served = int(result.get("customers_served", 1) or 0)
    if served >= 2:
        out.append(f"{served} клиентов — проверь подсчёт/двоение")
    ev = result.get("events", {}) or {}
    if ev.get("rudeness"):
        out.append("грубость — переслушай (телефон/персонал?)")
    if ev.get("fraud_attempt"):
        out.append("фрод — переслушай")
    # Длинный файл, но короткий транскрипт → возможно потеряли речь
    if wav_kb > 400 and len((transcript or "").split()) < 8 and status == "OK":
        out.append("длинный файл, мало слов — возможно недослышал")
    return "; ".join(out) or "—"


async def run_one(path: str) -> dict:
    with open(path, "rb") as f:
        wav = f.read()
    result = await analyze_audio_with_fallback(wav, None) or {}
    transcript = result.get("transcript", "") or ""
    ev = result.get("events", {}) or {}
    return {
        "file":     os.path.basename(path),
        "kb":       len(wav) // 1024,
        "status":   result.get("status", "—"),
        "score":    _final_score(result, transcript) if result.get("status") == "OK" else 0,
        "tone":     result.get("tone", "—"),
        "served":   result.get("customers_served", "—") if result.get("status") == "OK" else "—",
        "events":   ", ".join(k for k in _EVENT_KEYS if ev.get(k)) or "—",
        "summary":  (result.get("summary", "") or "")[:120],
        "flags":    _flags(result, transcript, len(wav) // 1024),
        "transcript": transcript[:300],
    }


def _table(rows: list[dict]) -> str:
    head = "| Файл | KB | Статус | Балл | Тон | Клиентов | События | Подозрительно |\n"
    head += "|---|---|---|---|---|---|---|---|\n"
    body = "".join(
        f"| {r['file']} | {r['kb']} | {r['status']} | {r['score']} | {r['tone']} | "
        f"{r['served']} | {r['events']} | {r['flags']} |\n"
        for r in rows
    )
    return head + body


async def main():
    arg = sys.argv[1] if len(sys.argv) > 1 else None
    files = _collect(arg)
    if not files:
        print("Нет аудио. Положи .wav в tests/samples/ или укажи путь:")
        print("  python scripts/hybrid_eval.py <папка_или_файл>")
        return

    print(f"Файлов: {len(files)} | ISSAI включён: {issai_stt.is_enabled()}\n")
    rows = []
    for path in files:
        print(f"… {os.path.basename(path)}")
        try:
            rows.append(await run_one(path))
        except Exception as e:
            rows.append({"file": os.path.basename(path), "kb": 0, "status": "ОШИБКА",
                         "score": 0, "tone": "—", "served": "—", "events": "—",
                         "summary": f"{type(e).__name__}: {e}", "flags": "ОШИБКА",
                         "transcript": ""})

    table = _table(rows)
    print("\n" + table)

    md = ["# STT-сводка по записям\n", table, "\n## Детали\n"]
    for r in rows:
        md.append(f"\n### {r['file']}  ({r['kb']} KB)\n")
        md.append(f"- статус: {r['status']} · балл: {r['score']} · тон: {r['tone']} · клиентов: {r['served']}\n")
        md.append(f"- события: {r['events']}\n")
        md.append(f"- подозрительно: {r['flags']}\n")
        md.append(f"- summary: {r['summary']}\n")
        md.append(f"- транскрипт (черновой): {r['transcript']}\n")
    md_text = "".join(md)
    with open("hybrid_result.md", "w", encoding="utf-8") as fp:
        fp.write(md_text)
    print("→ hybrid_result.md")

    # Сводка прямо на странице GitHub Actions (рендерится таблицей)
    gh_summary = os.getenv("GITHUB_STEP_SUMMARY")
    if gh_summary:
        with open(gh_summary, "a", encoding="utf-8") as fp:
            fp.write("# STT-сводка\n\n" + table)


if __name__ == "__main__":
    asyncio.run(main())
