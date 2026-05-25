# ════════════════════════════════════════════════════════════
#  Сервис: Анализ фраз и тона
# ════════════════════════════════════════════════════════════

import re
from typing import Optional


# ── Универсальные фразы ──────────────────────────────────────

GREETINGS = [
    r"добро пожаловать", r"здравствуйт", r"привет",
    r"доброе утро", r"добрый день", r"добрый вечер",
    r"рады вас видеть", r"чем могу помочь", r"слушаю вас",
    r"қош келдіңіз", r"сәлем", r"сәлеметсіз", r"саламатсыз",
    r"қайырлы таң", r"қайырлы күн", r"қайырлы кеш",
]

THANKS = [
    r"спасибо", r"благодар", r"пожалуйста", r"не за что",
    r"всегда рады", r"приятного", r"с удовольствием", r"обращайтесь",
    r"рахмет", r"сау болыңыз",
]

GOODBYE = [
    r"до свидания", r"до встречи", r"всего доброго",
    r"хорошего дня", r"удачного дня", r"приходите ещё",
    r"приходите еще", r"ждём вас снова", r"хорошего вечера",
    r"қайта келіңіз", r"жақсы күн", r"қош болыңыз",
]

BAD_LANGUAGE = [
    # Регексы для грубости намеренно пустые.
    # Грубость, тон, интонацию определяет GPT-4o-mini-audio через events.rudeness —
    # он понимает контекст и слышит как именно что-то сказано.
    # Тысячи ситуаций не покрыть словарём.
]

FRAUD = [
    # Regex для мошенничества намеренно пустой.
    # Мошенничество — это КОНЦЕПЦИЯ (перенаправление оплаты мимо кассы),
    # а не набор слов. GPT понимает эту концепцию на любом языке
    # через events.fraud_attempt. Regex здесь только создаёт ложные срабатывания.
    #
    # Исключение: Kaspi-фрод (диктовка номера телефона) обрабатывается
    # отдельно через kaspi_detector.py — там паттерн номера, не слова.
]

TONE_POSITIVE = [
    # Тон определяет GPT через поле "tone" — он слышит интонацию.
    # Регексы по словам не отражают реальный тон разговора.
]

TONE_NEGATIVE = [
    # Аналогично — GPT определяет негативный тон по контексту и интонации.
]

# ── Бонусные фразы по типам бизнеса ─────────────────────────

BONUS_PHRASES = {
    # Regex для допродаж намеренно пустой для всех типов бизнеса.
    #
    # Почему: упоминание слова "латте" или "комбо" в тексте
    # НЕ означает что кассир предложил допродажу.
    # Он мог сказать "у нас нет латте" или клиент сам спросил.
    #
    # Допродажа — это ДЕЙСТВИЕ кассира: он сам проактивно предложил
    # что-то дополнительное. Это концепция, а не слово.
    # GPT определяет это через upsell_attempt и events.upsell
    # на любом языке и в любом контексте.
    #
    # custom_phrases владельца (из настроек точки) всё ещё можно
    # передавать сюда как подсказку GPT — но не как основной детектор.
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
    Анализируем текст разговора.
    Возвращает словарь найденных категорий.
    """
    found = {}

    checks = [
        ("✅ Приветствие",    _compile(GREETINGS)),
        ("✅ Благодарность",  _compile(THANKS)),
        ("✅ Прощание",       _compile(GOODBYE)),
        ("⚠️ Грубость",      _compile(BAD_LANGUAGE)),
        ("🚨 МОШЕННИЧЕСТВО", _compile(FRAUD)),
        ("😊 Позитивный тон",_compile(TONE_POSITIVE)),
        ("😤 Негативный тон",_compile(TONE_NEGATIVE)),
    ]

    # Бонусные фразы для типа бизнеса
    bonus = BONUS_PHRASES.get(business_type, BONUS_PHRASES["coffee"])
    if custom_phrases:
        bonus = bonus + custom_phrases
    checks.append(("⭐ Допродажа/бонус", _compile(bonus)))

    for category, patterns in checks:
        hits = _search(patterns, text)
        if hits:
            found[category] = hits

    return found


def get_tone(found: dict) -> str:
    """Определяем итоговый тон: positive / negative / neutral."""
    if "😊 Позитивный тон" in found and "😤 Негативный тон" not in found:
        return "positive"
    if "😤 Негативный тон" in found:
        return "negative"
    return "neutral"


def calculate_score(found: dict, total_conversations: int = 1) -> float:
    """
    Оценка качества обслуживания 0–100.
    Учитывает наличие приветствия, благодарности, прощания,
    допродаж, тона и штрафует за нарушения.
    """
    score = 50.0  # базовая оценка

    if "✅ Приветствие"   in found: score += 15
    if "✅ Благодарность" in found: score += 10
    if "✅ Прощание"      in found: score += 10
    if "⭐ Допродажа/бонус" in found: score += 15
    if "😊 Позитивный тон" in found: score += 10

    if "⚠️ Грубость"      in found: score -= 25
    if "🚨 МОШЕННИЧЕСТВО" in found: score -= 50
    if "😤 Негативный тон" in found: score -= 10

    return max(0.0, min(100.0, score))
