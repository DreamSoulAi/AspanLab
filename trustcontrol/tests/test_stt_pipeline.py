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
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _mock_models(monkeypatch):
    """По умолчанию: ISSAI/Yandex выключены, аудио-модель и GPT — заглушки."""
    monkeypatch.setattr(issai_stt, "is_enabled", lambda: False)
    monkeypatch.setattr(A.yandex_stt, "is_enabled", lambda: False)
    async def _audio(*a, **k):  return {}
    async def _gpt(*a, **k):    return {}
    # По умолчанию первичный OpenAI-STT «молчит» — тесты проверяют маршрутизацию
    # фолбэков (ISSAI/Yandex/whisper). Кто тестирует первичку — мокает сам.
    async def _no_transcribe(*a, **k):  return ""
    monkeypatch.setattr(A, "analyze_audio", _audio)
    monkeypatch.setattr(A, "gpt_analyze", _gpt)
    monkeypatch.setattr(A, "_transcribe_audio", _no_transcribe)
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


@pytest.mark.parametrize("text", [
    # Реальные транскрипты со скринов кассы — рваные, но это живые сделки.
    "банан ааа сосын есеп содан берейін бе үш мың төрт жүз елу",
    "давайте тогда не морожить на трикафельді жиыншы қалады ғой мына "
    "ғашықшы баскер или карте ғашықшы мхм девяносто девяносто",
])
def test_real_noisy_screenshots_are_plausible(text):
    """Рваный шумный казахский с суммой/оплатой — НЕ мусор, не должен теряться."""
    assert A._is_plausible_conversation(text) is True


def test_card_payment_variants_detected():
    """Разные формы оплаты картой должны распознаваться как сделка."""
    assert A._looks_like_real_transaction("оплата на карте") is True
    assert A._looks_like_real_transaction("карта") is True
    assert A._looks_like_real_transaction("через терминал") is True
    assert A._looks_like_real_transaction("kaspi qr") is True


def test_false_ignore_with_amount_rescued_by_text_not_audio(_mock_models):
    """Явная сделка (озвучена сумма) + ложный IGNORE → спасаем через force_business
    text-GPT, БЕЗ дорогой переслушки аудио-модели (текст уже точный)."""
    _enable_issai(_mock_models, "есеп үш мың төрт жүз елу")          # есть сумма (мың)
    audio_called = {"n": 0}
    async def _gpt(text, force_business=False, **k):
        # Модель уважает force_business: на втором проходе возвращает OK.
        if force_business:
            return {"status": "OK", "is_business": True, "score": 60, "events": {},
                    "summary": "заказ + оплата", "tone": "neutral"}
        return {"status": "IGNORE", "is_business": False}            # ложный IGNORE
    async def _audio(*a, **k):
        audio_called["n"] += 1
        return {}
    _mock_models.setattr(A, "gpt_analyze", _gpt)
    _mock_models.setattr(A, "analyze_audio", _audio)
    res = _run(A.analyze_audio_with_fallback(b"RIFFxxxx", None, None))
    assert res["status"] == "OK", "сделка с суммой не должна теряться в IGNORE"
    assert audio_called["n"] == 0, "при явной сумме спасаем текстом, аудио-модель не зовём"


def test_no_stt_falls_back_to_audio_model(_mock_models):
    """Нет ISSAI/Yandex → аудио-модель как фолбэк (свежий звук)."""
    async def _audio(wav, **k):
        return {"status": "OK", "is_business": True, "transcript": "привет", "score": 80}
    _mock_models.setattr(A, "analyze_audio", _audio)
    res = _run(A.analyze_audio_with_fallback(b"RIFFxxxx", None, None))
    assert res["status"] == "OK" and res["score"] == 80


def test_audio_model_ignore_rescued_by_whisper(_mock_models):
    """Главный кейс со скринов: ISSAI пусто + аудио-модель сказала IGNORE →
    Whisper-1 расшифровывает речь и спасает разговор от потери."""
    _enable_issai(_mock_models, "")                       # ISSAI вернул пусто
    async def _audio(wav, **k):
        return {"status": "IGNORE", "is_business": False}  # аудио-модель сдалась
    async def _whisper(wav, lang=None, model=None):
        return "здравствуйте два капучино с вас тысяча двести спасибо"
    async def _gpt(text, **k):
        return {"status": "OK", "is_business": True, "score": 78, "events": {},
                "summary": "две чашки капучино", "tone": "neutral"}
    _mock_models.setattr(A, "analyze_audio", _audio)
    _mock_models.setattr(A, "_transcribe_audio", _whisper)
    _mock_models.setattr(A, "gpt_analyze", _gpt)
    res = _run(A.analyze_audio_with_fallback(b"RIFFxxxx", None, None))
    assert res["status"] == "OK", "Whisper-1 должен спасти разговор когда ISSAI пусто и аудио-модель IGNORE"


def test_all_engines_silent_stays_ignore(_mock_models):
    """Если ВСЕ движки молчат (ISSAI пусто, аудио IGNORE, Whisper тоже пусто) —
    это честная тихая/нерелевантная запись, остаётся IGNORE."""
    _enable_issai(_mock_models, "")
    async def _audio(wav, **k):
        return {"status": "IGNORE", "is_business": False}
    async def _whisper(wav, lang=None, model=None):
        return ""                                          # Whisper тоже ничего
    _mock_models.setattr(A, "analyze_audio", _audio)
    _mock_models.setattr(A, "_transcribe_audio", _whisper)
    res = _run(A.analyze_audio_with_fallback(b"RIFFxxxx", None, None))
    assert res["status"] == "IGNORE"


# ── Защита от галлюцинаций-зацикливаний STT ───────────────────────────────────

def test_strip_loop_collapses_repeated_token():
    """«Сөйтеті ×40» (петля gpt-4o-transcribe на казахском) схлопывается до одной."""
    real = "сеттегін алдырмаймай деді ақша предлагает етіп"
    loop = " ".join(["Сөйтеті."] * 40)
    cleaned = A._strip_repeat_loops(real + " " + loop)
    assert cleaned.lower().count("сөйтеті") <= 2
    assert "алдырмаймай" in cleaned          # реальная речь до петли сохранена


def test_strip_loop_collapses_repeated_phrase():
    """Петля из фразы 2-3 слова тоже схлопывается."""
    loop = " ".join(["касса в Казахстане"] * 10)
    cleaned = A._strip_repeat_loops(loop)
    assert cleaned.lower().count("касса") <= 2


def test_strip_loop_keeps_normal_text():
    """Нормальная речь без петель не трогается."""
    txt = "здравствуйте два капучино с вас тысяча двести спасибо до свидания"
    assert A._strip_repeat_loops(txt) == txt


def test_strip_loop_keeps_natural_short_repeats():
    """Естественный повтор «да да» (2 раза) — не петля, не режем агрессивно."""
    txt = "да да один кофе пожалуйста"
    assert "один кофе" in A._strip_repeat_loops(txt)


# ── Посегментная логика: customers_served (несколько клиентов в одной записи) ──

def test_normalize_result_carries_customers_served():
    """Новое поле customers_served должно доходить до результата для отчёта."""
    gpt = {"status": "OK", "is_business": True, "score": 70, "events": {},
           "summary": "Обслужено 2 клиента", "tone": "neutral", "customers_served": 2}
    res = A._normalize_text_result(gpt, "здравствуйте ... спасибо", "ru")
    assert res["customers_served"] == 2


def test_normalize_result_defaults_customers_served_to_one():
    """Если модель не вернула счётчик — считаем минимум 1 клиента."""
    gpt = {"status": "OK", "is_business": True, "score": 70, "events": {},
           "summary": "заказ", "tone": "neutral"}
    res = A._normalize_text_result(gpt, "два кофе с вас 900", "ru")
    assert res["customers_served"] == 1


def _mock_gpt_client(monkeypatch, payload: dict):
    """Подменяет ответ OpenAI-клиента в gpt_analyzer фиксированным JSON."""
    import json as _json
    from backend.services import gpt_analyzer as G

    class _Msg:      content = _json.dumps(payload)
    class _Choice:   message = _Msg()
    class _Resp:     choices = [_Choice()]
    async def _create(**k):  return _Resp()

    monkeypatch.setattr(G.settings, "OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(G.client.chat.completions, "create", _create)
    return G


def test_gpt_analyze_defaults_customers_served(monkeypatch):
    """gpt_analyze проставляет customers_served=1 если GPT его не вернул."""
    G = _mock_gpt_client(monkeypatch, {
        "status": "OK", "is_business": True, "score": 65, "events": {},
        "summary": "заказ", "tone": "neutral"})
    res = _run(G.gpt_analyze("здравствуйте два кофе с вас девятьсот спасибо"))
    assert res["status"] == "OK"
    assert res["customers_served"] == 1


def test_gpt_analyze_keeps_multiple_customers(monkeypatch):
    """gpt_analyze сохраняет customers_served когда обслужено несколько клиентов."""
    G = _mock_gpt_client(monkeypatch, {
        "status": "OK", "is_business": True, "score": 65, "events": {},
        "summary": "Обслужено 3 клиента", "tone": "neutral", "customers_served": 3})
    res = _run(G.gpt_analyze("длинная запись с тремя клиентами подряд и болтовнёй"))
    assert res["customers_served"] == 3


def test_gpt_analyze_order_inside_gossip_stays_ok(monkeypatch):
    """Ключевой кейс: заказ внутри болтовни персонала → OK, не PERSONAL."""
    G = _mock_gpt_client(monkeypatch, {
        "status": "OK", "is_business": True, "score": 60, "events": {"greeting": True},
        "summary": "Болтовня персонала + 1 заказ", "tone": "neutral",
        "customers_served": 1})
    res = _run(G.gpt_analyze("эй где соус ... здравствуйте два донера с вас 1600 спасибо ... ну а потом что"))
    assert res["status"] == "OK"
    assert res.get("is_business") is True


# ── _merge_transcripts: гибридное объединение двух STT ────────────────────────

def test_merge_empty_issai_returns_openai():
    """Если ISSAI пустой — возвращаем OpenAI без GPT-вызова."""
    res = _run(A._merge_transcripts("", "два кофе с вас 900"))
    assert res == "два кофе с вас 900"


def test_merge_empty_openai_returns_issai():
    """Если OpenAI пустой — возвращаем ISSAI без GPT-вызова."""
    res = _run(A._merge_transcripts("бір кофе ия рахмет", ""))
    assert res == "бір кофе ия рахмет"


def test_merge_both_empty_returns_empty():
    """Оба пустые — возвращаем пустую строку."""
    res = _run(A._merge_transcripts("", ""))
    assert res == ""


def test_merge_identical_texts_no_gpt_call():
    """Одинаковые тексты → возвращаем без дополнительного GPT-вызова."""
    text = "здравствуйте два капучино с вас тысяча двести"
    res = _run(A._merge_transcripts(text, text))
    assert res == text


def test_merge_one_word_issai_returns_openai():
    """Один вариант из одного слова — берём более длинный без GPT-вызова."""
    res = _run(A._merge_transcripts("ия", "рожок или стаканчик с вас 500"))
    assert "рожок" in res


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


def test_score_polite_visit_clears_base():
    """Вежливый разговор (приветствие + прощание) должен заметно превышать базу 60,
    чтобы не сливаться с пустыми визитами на дашборде."""
    polite = calculate_score(events={"greeting": True, "farewell": True})
    assert polite >= 75   # 60 + 10 + 8 = 78
    assert polite > calculate_score(events={})   # явно выше пустого визита


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
