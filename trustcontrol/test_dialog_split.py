#!/usr/bin/env python3
# Шаг 3 — тест умного разбиения диалогов на примере из плана:
#   кофейня, 3 клиента + болтовня персонала.
# Проверяем БЕЗ сети (мокаем GPT-вызовы):
#   A. split_into_dialogues корректно парсит/валидирует ответ модели.
#   B. _process_submission создаёт ОТДЕЛЬНЫЙ Report на клиента, PERSONAL — НЕ создаёт.
#   C. is_primary=True ровно у ОДНОГО Report (биллинг «по записям»).
#   D. PERSONAL с признаком фрода (сговор персонала) — анализируется (не пропуск).
#   E. Фолбэк: split вернул [] → ровно один Report на весь транскрипт.
#
# Запуск:
#   SECRET_KEY=... DATABASE_URL=... OPENAI_API_KEY=test python test_dialog_split.py
import os
os.environ.setdefault("SECRET_KEY", "t" * 40)
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./test_split.db")
os.environ.setdefault("OPENAI_API_KEY", "test-key")

import sys
import json
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import backend.api.reports as R
import backend.services.dialog_splitter as DS

# ── Пример из плана: кофейня, 3 клиента + болтовня персонала (рус+каз) ──
COFFEE = (
    "Здравствуйте, что будете? Латте большой пожалуйста. "
    "Триста пятьдесят тенге, QR или наличными? QR. [тишина] "
    "Сәлем! Маған капучино екі дана. Жеті жүз тенге болады. QR ма? Иә, QR. "
    "Рахмет, сау болыңыз! Айгуль, когда обед? Через полчаса наверное. Ладно. [тишина] "
    "Добрый день! Что вам? Американо без сахара. Двести пятьдесят. Наличкой? "
    "Да. Вот сдача. Спасибо, до свидания! Вам спасибо."
)

# Что «вернула бы» модель разбиения (4 сегмента: 3 SERVICE + 1 PERSONAL)
COFFEE_SEGMENTS_RAW = {
    "dialogues": [
        {"text": "Здравствуйте, что будете? Латте большой пожалуйста. Триста пятьдесят тенге, QR или наличными? QR.",
         "type": "SERVICE", "start_marker": "Здравствуйте", "end_marker": "QR"},
        {"text": "Сәлем! Маған капучино екі дана. Жеті жүз тенге болады. QR ма? Иә, QR. Рахмет, сау болыңыз!",
         "type": "SERVICE", "start_marker": "Сәлем", "end_marker": "сау болыңыз"},
        {"text": "Айгуль, когда обед? Через полчаса наверное. Ладно.",
         "type": "PERSONAL", "start_marker": None, "end_marker": None},
        {"text": "Добрый день! Что вам? Американо без сахара. Двести пятьдесят. Наличкой? Да. Вот сдача. Спасибо, до свидания! Вам спасибо.",
         "type": "SERVICE", "start_marker": "Добрый день", "end_marker": "до свидания"},
    ]
}


def _fake_openai_resp(payload: dict):
    resp = MagicMock()
    choice = MagicMock()
    choice.message.content = json.dumps(payload, ensure_ascii=False)
    resp.choices = [choice]
    return resp


async def _fake_analyze(wav_bytes=None, transcript_text=None, **kw):
    """Имитация analyze_audio_with_fallback (текстовый режим) — всегда рабочий разговор."""
    txt = (transcript_text or "").strip()
    return {
        "status": "OK", "is_business": True, "transcript": txt,
        "events": {}, "summary": "ok", "tone": "neutral",
        "speakers": ["кассир", "клиент"], "score": 70,
    }


def _make_persist_recorder():
    calls = []

    async def _fake_persist(*, result, transcript, is_primary, audio_fraud_number, **common):
        calls.append({
            "transcript": transcript,
            "is_primary": is_primary,
            "fraud": audio_fraud_number,
        })
        return len(calls)

    return calls, _fake_persist


async def _run_submission(segments_return):
    """Гоняем _process_submission с мокнутыми split/analyze/persist, считаем Report'ы."""
    calls, fake_persist = _make_persist_recorder()
    with patch.object(R, "analyze_audio_with_fallback", new=AsyncMock(side_effect=_fake_analyze)), \
         patch.object(R, "split_into_dialogues", new=AsyncMock(return_value=segments_return)), \
         patch.object(R, "_persist_report", new=AsyncMock(side_effect=fake_persist)):
        await R._process_submission(
            location_id=1, wav_bytes=None, transcript_text=COFFEE,
            language="ru", audio_size_kb=0, business_type="coffee",
            custom_phrases=[], telegram_chat=None, location_name="Кофейня Тест",
        )
    return calls


# ── Сегменты в формате, который отдаёт split_into_dialogues (с has_service_markers) ──
def _norm_segments(raw):
    out = []
    for s in raw["dialogues"]:
        out.append({
            "text": s["text"], "type": s["type"],
            "start_marker": s.get("start_marker"), "end_marker": s.get("end_marker"),
            "has_service_markers": bool(s.get("start_marker") or s.get("end_marker")),
        })
    return out


async def test_a_splitter_parsing():
    with patch.object(DS.client.chat.completions, "create",
                      new=AsyncMock(return_value=_fake_openai_resp(COFFEE_SEGMENTS_RAW))):
        out = await DS.split_into_dialogues(COFFEE, "coffee", "mixed", None)
    types = [s["type"] for s in out]
    ok = (len(out) == 4 and types.count("SERVICE") == 3 and types.count("PERSONAL") == 1)
    print(f"A. splitter: {len(out)} сегментов, типы={types} → {'✅' if ok else '❌'}")
    return ok


async def test_b_three_reports_personal_skipped():
    calls = await _run_submission(_norm_segments(COFFEE_SEGMENTS_RAW))
    n = len(calls)
    personal_text = "Айгуль"
    personal_saved = any(personal_text in c["transcript"] for c in calls)
    ok = (n == 3 and not personal_saved)
    print(f"B. Report'ов создано: {n} (ожидали 3), PERSONAL сохранён: {personal_saved} → {'✅' if ok else '❌'}")
    for i, c in enumerate(calls, 1):
        print(f"     #{i} primary={c['is_primary']} | {c['transcript'][:55]}…")
    return ok


async def test_c_one_primary():
    calls = await _run_submission(_norm_segments(COFFEE_SEGMENTS_RAW))
    primaries = sum(1 for c in calls if c["is_primary"])
    ok = (primaries == 1 and calls[0]["is_primary"] is True)
    print(f"C. is_primary=True ровно у одного Report: {primaries} → {'✅' if ok else '❌'}")
    return ok


async def test_d_personal_with_fraud_analyzed():
    # PERSONAL-сегмент со сговором персонала: «переведи на 8 707…» — НЕ пропускаем.
    raw = {
        "dialogues": [
            {"text": "Здравствуйте, латте. Триста пятьдесят, QR. Спасибо.",
             "type": "SERVICE", "start_marker": "Здравствуйте", "end_marker": "Спасибо"},
            {"text": "Слушай, аппарат глючит, переведи на 8 707 999 88 77, потом разберёмся.",
             "type": "PERSONAL", "start_marker": None, "end_marker": None},
        ]
    }
    calls = await _run_submission(_norm_segments(raw))
    fraud_seg_saved = any("999 88 77" in c["transcript"] for c in calls)
    ok = (len(calls) == 2 and fraud_seg_saved)
    print(f"D. PERSONAL с признаком перевода проанализирован: saved={fraud_seg_saved}, "
          f"всего={len(calls)} → {'✅' if ok else '❌'}")
    return ok


async def test_e_fallback_single_report():
    # split вернул [] (короткая запись / отказ / ошибка) → один Report на всё.
    calls = await _run_submission([])
    ok = (len(calls) == 1 and calls[0]["is_primary"] is True
          and calls[0]["transcript"] == COFFEE.strip())
    print(f"E. Фолбэк (split=[]) → один Report на весь транскрипт: {len(calls)} → {'✅' if ok else '❌'}")
    return ok


async def main():
    results = []
    for t in (test_a_splitter_parsing, test_b_three_reports_personal_skipped,
              test_c_one_primary, test_d_personal_with_fraud_analyzed,
              test_e_fallback_single_report):
        results.append(await t())
    print("=" * 70)
    passed = sum(results)
    print(f"СВОДКА: {passed}/{len(results)} тестов пройдено")
    print("=" * 70)
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
