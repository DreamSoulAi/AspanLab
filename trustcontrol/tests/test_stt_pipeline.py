# ════════════════════════════════════════════════════════════
#  Тесты логики STT-пайплайна и оценки.
#
#  Проверяют ЛОГИКУ (без реальных моделей/сети):
#    • когда запись считается мусором, а когда настоящим разговором
#    • что слабый распознаватель НЕ может ложно «ветировать» разговор
#    • маршрутизацию analyze_audio_with_fallback (модели замоканы)
#    • детерминированный движок оценки calculate_score
#    • контекст разговора, суммы из транскрипта
#
#  Запуск:  pytest tests/test_stt_pipeline.py -v
# ════════════════════════════════════════════════════════════

import asyncio
import pytest

from backend.services import audio_analyzer as A
from backend.services import issai_stt
from backend.services import context_analyzer as C
from backend.services.analyzer import (
    calculate_score, get_tone, FRAUD_HARD_THRESHOLD, FRAUD_SOFT_THRESHOLD,
)
from backend.services.pos_matcher import extract_amounts


# ── _is_plausible_conversation: что разговор, а что мусор ──────────────────────

@pytest.mark.parametrize("text", [
    "",
    "   ",
    "әлім кәлі",                       # 2 слова каши (файл с матом)
    "да да да да да да да",            # повтор-галлюцинация: много слов, мало разных
])
def test_implausible_text_rejected(text):
    assert A._is_plausible_conversation(text) is False


@pytest.mark.parametrize("text", [
    "алты жүз тоқсан теңге болады",                  # озвучена сумма (каз)
    "екі мың төрт жүз жетпіс",                       # сумма (каз) — файл с ценой
    "здравствуйте что будете заказывать",           # маркер сервиса (приветствие+заказ)
    "сәлеметсіз бе не аласыз",                       # каз. приветствие+заказ
    "так с вас тысяча двести спасибо за покупку",    # оплата + прощание
    "один капучино и круассан пожалуйста на вынос",  # связный заказ (>=6 разных слов)
])
def test_plausible_conversations_accepted(text):
    assert A._is_plausible_conversation(text) is True


def test_payment_signal_detected():
    assert A._looks_like_real_transaction("итого 1500") is True
    assert A._looks_like_real_transaction("картамен төлейміз") is True
    assert A._looks_like_real_transaction("просто болтают о погоде") is False


# ── issai_stt.is_garbage: длинное аудио + мало слов = мусор ────────────────────

def test_garbage_long_audio_few_words():
    assert issai_stt.is_garbage("әлім кәлі", 66) is True          # 2 слова / 66с
    assert issai_stt.is_garbage("алты жүз тоқсан болды", 30) is False  # 4 слова — ок
    assert issai_stt.is_garbage("спасибо", 5) is False            # короткое аудио — ок
    assert issai_stt.is_garbage("", 60) is False                  # пусто — не мусор


# ── Маршрутизация analyze_audio_with_fallback (модели замоканы) ────────────────
# Реальный (слитый) поток: ISSAI/Yandex → дешёвый text-GPT; аудио-модель только
# как фолбэк / редкая страховка от ложного IGNORE. Проверяем именно эту логику.

def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


@pytest.fixture(autouse=True)
def _mock_models(monkeypatch):
    """По умолчанию: ISSAI/Yandex выключены, аудио-модель и GPT — заглушки."""
    monkeypatch.setattr(issai_stt, "is_enabled", lambda: False)
    monkeypatch.setattr(A.yandex_stt, "is_enabled", lambda: False)
    async def _audio(*a, **k):  return {}
    async def _gpt(*a, **k):    return {}
    monkeypatch.setattr(A, "analyze_audio", _audio)
    monkeypatch.setattr(A, "gpt_analyze", _gpt)
    return monkeypatch


def _enable_issai(mp, text):
    mp.setattr(issai_stt, "is_enabled", lambda: True)
    async def _issai(wav, **k):  return text
    mp.setattr(issai_stt, "transcribe", _issai)


def test_text_mode_uses_text_gpt(_mock_models):
    async def _gpt(text, **k):
        return {"status": "OK", "is_business": True, "score": 70, "events": {},
                "summary": "ок", "tone": "neutral"}
    _mock_models.setattr(A, "gpt_analyze", _gpt)
    res = _run(A.analyze_audio_with_fallback(None, "здравствуйте два кофе с вас 900", None))
    assert res["status"] == "OK"
    assert "два кофе" in res["transcript"]


def test_stt_text_goes_to_cheap_text_gpt(_mock_models):
    """Когда ISSAI дал текст — анализ через дешёвый text-GPT, аудио-модель НЕ зовём."""
    _enable_issai(_mock_models, "здравствуйте один латте картой спасибо")
    audio_called = {"n": 0}
    async def _audio(*a, **k):
        audio_called["n"] += 1
        return {}
    async def _gpt(text, **k):
        return {"status": "OK", "is_business": True, "score": 75, "events": {},
                "summary": "латте", "tone": "neutral"}
    _mock_models.setattr(A, "analyze_audio", _audio)
    _mock_models.setattr(A, "gpt_analyze", _gpt)
    res = _run(A.analyze_audio_with_fallback(b"RIFFxxxx", None, None))
    assert res["status"] == "OK"
    assert audio_called["n"] == 0, "аудио-модель не должна вызываться когда есть STT-текст (экономия)"


def test_false_ignore_on_plausible_text_rescued_by_audio(_mock_models):
    """Главный кейс: text-GPT ошибочно сказал IGNORE на обрывке с суммой →
    страховка слушает звук аудио-моделью и спасает разговор."""
    _enable_issai(_mock_models, "екі мың төрт жүз жетпіс теңге")   # озвучена сумма
    async def _gpt(text, **k):
        return {"status": "IGNORE", "is_business": False}          # ложный IGNORE
    async def _audio(wav, **k):
        return {"status": "OK", "is_business": True, "transcript": "екі мың...", "score": 60}
    _mock_models.setattr(A, "gpt_analyze", _gpt)
    _mock_models.setattr(A, "analyze_audio", _audio)
    res = _run(A.analyze_audio_with_fallback(b"RIFFxxxx", None, None))
    assert res["status"] == "OK", "разговор с озвученной суммой не должен теряться в IGNORE"


def test_garbage_text_ignore_not_rescued(_mock_models):
    """Каша (2 слова, неправдоподобно) + IGNORE → остаётся IGNORE, аудио не зовём."""
    _enable_issai(_mock_models, "әлім кәлі")
    audio_called = {"n": 0}
    async def _gpt(text, **k):
        return {"status": "IGNORE", "is_business": False}
    async def _audio(*a, **k):
        audio_called["n"] += 1
        return {}
    _mock_models.setattr(A, "gpt_analyze", _gpt)
    _mock_models.setattr(A, "analyze_audio", _audio)
    res = _run(A.analyze_audio_with_fallback(b"RIFFxxxx", None, None))
    assert res["status"] == "IGNORE"
    assert audio_called["n"] == 0, "на неправдоподобной каше страховку не запускаем"


def test_no_stt_falls_back_to_audio_model(_mock_models):
    """Нет ISSAI/Yandex → аудио-модель как фолбэк (свежий звук)."""
    async def _audio(wav, **k):
        return {"status": "OK", "is_business": True, "transcript": "привет", "score": 80}
    _mock_models.setattr(A, "analyze_audio", _audio)
    res = _run(A.analyze_audio_with_fallback(b"RIFFxxxx", None, None))
    assert res["status"] == "OK" and res["score"] == 80


# ── calculate_score: детерминированный движок ─────────────────────────────────

def test_score_hard_fraud_floors_to_5():
    s = calculate_score(events={"fraud_attempt": True}, fraud_confidence=FRAUD_HARD_THRESHOLD)
    assert s == 5


def test_score_soft_fraud_penalised_not_floored():
    s = calculate_score(events={"fraud_attempt": True}, fraud_confidence=FRAUD_SOFT_THRESHOLD)
    assert 5 < s < 60   # штраф есть, но не пол


def test_score_rudeness_single_penalty():
    base = calculate_score(events={})
    rude = calculate_score(events={"rudeness": True})
    assert base - rude == 30   # ровно один штраф −30


def test_score_short_visit_not_punished():
    s = calculate_score(events={}, is_short=True)
    assert s >= 55   # тихий короткий визит = нейтрально, не минус


def test_score_clamped_0_100():
    hi = calculate_score(events={"greeting": True, "farewell": True, "upsell": True,
                                 "issue_resolved": True}, tone="positive",
                         customer_satisfaction=5, energy_level=5)
    assert 0 <= hi <= 100


def test_score_missing_greeting_is_not_penalty():
    """Отсутствие приветствия НЕ штраф (микрофон режет начало)."""
    with_g = calculate_score(events={"greeting": True})
    without = calculate_score(events={})
    assert with_g >= without          # приветствие — только бонус
    assert without == 60              # база, без штрафа


# ── get_tone ──────────────────────────────────────────────────────────────────

def test_get_tone_prefers_gpt_then_events():
    assert get_tone("positive") == "positive"
    assert get_tone("", {"rudeness": True}) == "negative"
    assert get_tone("garbage", {}) == "neutral"


# ── context_analyzer ──────────────────────────────────────────────────────────

def test_context_customer_service_with_payment():
    ctx = C.analyze_context(
        transcript="здравствуйте два кофе с вас 900 спасибо за покупку",
        events={"greeting": True}, speakers=[{"role": "cashier"}, {"role": "customer"}],
        has_pos_nearby=False,
    )
    assert ctx["context"] == "customer_service"


def test_context_internal_talk_no_markers():
    ctx = C.analyze_context(
        transcript="ну ты чего вчера делал после смены",
        events={}, speakers=[{"role": "cashier"}], has_pos_nearby=False,
    )
    assert ctx["context"] in ("internal_talk", "unknown")


def test_payment_talk_detection():
    assert C.has_payment_talk("итого с вас 500") is True
    assert C.has_payment_talk("картамен төлейміз") is True
    assert C.has_payment_talk("хорошая погода сегодня") is False


# ── extract_amounts (POS-матчер) ──────────────────────────────────────────────

def test_extract_amounts_digits():
    assert 1500.0 in extract_amounts("итого 1500 тенге")
    assert 690.0 in extract_amounts("с вас 690")


def test_extract_amounts_words_are_components_not_composed():
    """ИЗВЕСТНОЕ ОГРАНИЧЕНИЕ: суммы словами разбираются по частям, НЕ складываются.
    'шестьсот девяносто' → [90, 600], а не 690. Цена, названная словами, может
    не совпасть с чеком в POS-матчинге. Тест фиксирует текущее поведение —
    если позже добавим композицию чисел, тест нужно обновить."""
    amounts = extract_amounts("шестьсот девяносто")
    assert 600.0 in amounts and 90.0 in amounts
    assert 690.0 not in amounts
    # каз «мың»=1000 распознаётся как компонент
    assert any(a >= 1000 for a in extract_amounts("екі мың теңге"))
