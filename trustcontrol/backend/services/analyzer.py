# ════════════════════════════════════════════════════════════
#  Сервис: Анализ фраз и тона
#
#  Два уровня анализа:
#    1. Regex — быстрая проверка приветствий/прощаний на ru/kk
#    2. GPT   — основной анализ тона, грубости, мошенничества,
#               допродаж на любом языке (через events и score)
#
#  BONUS_PHRASES — не regex-детектор, а контекст для GPT:
#    "Для этого типа бизнеса целевые допродажи — это X, Y, Z.
#     Предложил ли кассир что-то из этого?"
# ════════════════════════════════════════════════════════════

import re
from typing import Optional


# ── Универсальные фразы (резервный слой для ru/kk) ───────────

GREETINGS = [
    r"добро пожаловать", r"здравствуйт", r"привет",
    r"доброе утро", r"добрый день", r"добрый вечер",
    r"рады вас видеть", r"чем могу помочь", r"слушаю вас",
    r"қош келдіңіз", r"сәлем", r"сәлеметсіз", r"саламатсыз",
    r"қайырлы таң", r"қайырлы күн", r"қайырлы кеш",
    r"жақсысыз ба", r"қалайсыз", r"не аласыз", r"не берейін",
    r"ассалаумағалейкум", r"уағалейкумассалам",
]

THANKS = [
    r"спасибо", r"благодар", r"пожалуйста", r"не за что",
    r"всегда рады", r"приятного", r"с удовольствием", r"обращайтесь",
    r"рахмет", r"сау болыңыз", r"рақмет",
]

GOODBYE = [
    r"до свидания", r"до встречи", r"всего доброго",
    r"хорошего дня", r"удачного дня", r"приходите ещё",
    r"приходите еще", r"ждём вас снова", r"хорошего вечера",
    r"қайта келіңіз", r"жақсы күн", r"қош болыңыз",
    r"сау болыңыз", r"хош", r"жақсы қалыңыз",
]

# Грубость и мошенничество — только через GPT (events.rudeness / events.fraud_attempt).
# Тысяча ситуаций, контекст и интонация важнее конкретных слов.
BAD_LANGUAGE = []
FRAUD = []


# ── Целевые допродажи по типу бизнеса ────────────────────────
#
# Это НЕ regex-детектор. Это справочник что владелец хочет
# отслеживать как upsell для своего типа бизнеса.
#
# Используется двумя способами:
#   1. Передаётся в GPT-промпт как контекст: "для этой точки
#      целевые допродажи — это [список]. Предложил ли кассир?"
#   2. Позволяет давать конкретные рекомендации в отчёте:
#      "Кассир не предложил карту лояльности (целевая допродажа)"
#
# GPT всё равно остаётся основным детектором через upsell_attempt —
# этот список только уточняет ЧТО именно считать upsell для бизнеса.

BONUS_PHRASES = {
    "coffee": [
        "сироп", "карта лояльности", "десерт", "круассан",
        "маффин", "выпечка", "двойной эспрессо", "большой размер",
    ],
    "gas": [
        "масло", "незамерзайка", "омыватель", "клубная карта",
        "автохимия", "полный бак",
    ],
    "fastfood": [
        "напиток", "соус", "комбо", "картошка фри",
        "десерт", "увеличенная порция", "что-нибудь ещё",
    ],
    "cafe": [
        "десерт", "гарнир", "закуска", "блюдо дня",
        "аперитив", "специальное предложение",
    ],
    "beauty": [
        "маска", "уход", "профессиональный шампунь",
        "следующая запись", "доп. процедура",
    ],
    "shop": [
        "карта скидок", "акция", "новинка", "пакет",
        "страховка", "расширенная гарантия",
    ],
    "fitness": [
        "персональный тренер", "групповые занятия",
        "спортивное питание", "продление абонемента", "сауна",
    ],
    "hotel": [
        "завтрак", "ранний заезд", "поздний выезд",
        "трансфер", "спа", "экскурсия",
    ],
}


def _compile(patterns: list[str]) -> list[re.Pattern]:
    return [re.compile(p, re.IGNORECASE | re.UNICODE) for p in patterns]


def _search(patterns: list[re.Pattern], text: str) -> list[str]:
    hits = []
    for pat in patterns:
        m = pat.search(text)
        if m:
            hits.append(m.group(0))
    return hits


def analyze(
    text: str,
    business_type: str = "coffee",
    custom_phrases: list[str] = None,
) -> dict:
    """
    Быстрый regex-анализ: только приветствия, благодарности, прощания.
    Грубость, мошенничество, тон — через GPT events (не здесь).
    """
    found = {}

    checks = [
        ("✅ Приветствие",   _compile(GREETINGS)),
        ("✅ Благодарность", _compile(THANKS)),
        ("✅ Прощание",      _compile(GOODBYE)),
    ]

    for category, patterns in checks:
        hits = _search(patterns, text)
        if hits:
            found[category] = hits

    return found


def get_tone(gpt_tone: str, events: dict = None) -> str:
    """
    Определяет итоговый тон разговора.
    Основа — GPT tone (он слышит интонацию).
    Fallback — анализ events если GPT не вернул тон.
    """
    if gpt_tone in ("positive", "negative", "neutral"):
        return gpt_tone

    # Fallback по events если GPT не вернул валидный тон
    if events:
        if events.get("fraud_attempt") or events.get("rudeness"):
            return "negative"
        if events.get("greeting") and events.get("farewell") and events.get("issue_resolved"):
            return "positive"

    return "neutral"


def calculate_score(
    gpt_score: int | None,
    events: dict = None,
    has_greeting: bool = False,
    has_goodbye: bool = False,
    has_bonus: bool = False,
    has_bad: bool = False,
    has_fraud: bool = False,
    tone: str = "neutral",
    track_upsell: bool = True,
    track_greeting: bool = True,
    track_goodbye: bool = True,
) -> float:
    """
    Итоговая оценка качества обслуживания 0–100.

    track_* флаги: если владелец отключил отслеживание параметра,
    он не влияет на оценку (ни плюс ни минус).
    """
    events = events or {}

    # Жёсткое бизнес-правило: фрод — всегда критично
    if has_fraud or events.get("fraud_attempt"):
        return max(0.0, min(10.0, (gpt_score or 50) * 0.1))

    if gpt_score is not None:
        score = float(gpt_score)
        if has_bad or events.get("rudeness"):
            score = max(0.0, score - 10)
        # База 50 для любого нормального разговора без нарушений
        if score < 50 and not has_bad and not has_fraud \
                and not events.get("rudeness") and not events.get("fraud_attempt"):
            score = 50.0
        return max(0.0, min(100.0, score))

    # Fallback: GPT не ответил, считаем по флагам
    score = 50.0
    if track_greeting and has_greeting:                     score += 15
    if track_goodbye and has_goodbye:                       score += 10
    if track_upsell and (has_bonus or events.get("upsell")): score += 15
    if tone == "positive":                                  score += 10
    if events.get("issue_resolved"):                        score += 10
    if has_bad or events.get("rudeness"):                   score -= 25
    if tone == "negative":                                  score -= 10

    return max(0.0, min(100.0, score))


def get_target_upsells(business_type: str, custom_phrases: list[str] = None) -> list[str]:
    """
    Возвращает целевые допродажи для типа бизнеса.
    Используется как контекст для GPT-промпта и для детальных отчётов.
    """
    targets = BONUS_PHRASES.get(business_type, [])
    if custom_phrases:
        targets = targets + custom_phrases
    return targets
