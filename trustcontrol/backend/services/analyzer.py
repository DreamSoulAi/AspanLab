# ════════════════════════════════════════════════════════════
#  Сервис: Анализ фраз + единый движок оценки
#
#  • analyze()        — regex-резерв приветствий/прощаний (ru/kk),
#                       срабатывает когда GPT не дал события
#  • get_tone()       — итоговый тон (GPT-тон → fallback по events)
#  • calculate_score()— ЕДИНЫЙ прозрачный движок оценки 0-100.
#                       Источник истины для отчёта, дашборда, аналитики
#                       и тревог. GPT даёт сигналы — движок считает балл.
# ════════════════════════════════════════════════════════════

import re


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


# ── Пороги уверенности по фроду ──────────────────────────────
# GPT в промпте различает явный фрод (90-100) и косвенный намёк (50-89).
# Балл/тревога должны это уважать, иначе один неуверенный сигнал = разнос.
FRAUD_HARD_THRESHOLD = 75   # явный фрод → балл в пол, критическая тревога
FRAUD_SOFT_THRESHOLD = 50   # косвенное подозрение → штраф, но без обнуления


def calculate_score(
    events: dict = None,
    *,
    tone: str = "neutral",
    fraud_confidence: int = 0,
    customer_satisfaction: int | None = None,
    energy_level: int | None = None,
    track_greeting: bool = True,
    track_goodbye: bool = True,
    track_upsell: bool = True,
    is_short: bool = False,
) -> int:
    """
    ЕДИНЫЙ прозрачный движок оценки качества обслуживания (0-100).

    GPT даёт только СИГНАЛЫ (events, tone, fraud_confidence, satisfaction,
    energy) — здесь из них детерминированно считается финальный балл.
    Это единственный источник истины: один и тот же балл идёт в отчёт,
    дашборд, аналитику сотрудников и тревоги.

    Принципы:
      • База 60 — нормальный визит без эксцессов = крепкая оценка.
      • Приветствие / прощание / допродажа — БОНУС за наличие, НИКОГДА не
        штраф за отсутствие (микрофон обрезает начало, тихие визиты — норма).
      • track_* флаги: отключённый владельцем параметр не влияет вообще.
      • Грубость штрафуется ОДИН раз (без двойных штрафов).
      • Фрод критичен только при высокой уверенности (порог confidence).
      • Короткий / тихий визит → нейтральный балл, без штрафов за краткость.
    """
    events = events or {}

    greeting = bool(events.get("greeting"))
    farewell = bool(events.get("farewell"))
    upsell   = bool(events.get("upsell"))
    rudeness = bool(events.get("rudeness"))
    resolved = bool(events.get("issue_resolved"))
    fraud    = bool(events.get("fraud_attempt"))

    # ── Явный фрод (высокая уверенность) → балл в пол ────────
    if fraud and fraud_confidence >= FRAUD_HARD_THRESHOLD:
        return 5

    score = 60.0

    # ── Позитивные сигналы — только бонусы за наличие ────────
    # Вежливость должна заметно поднимать балл над базой: вежливый разговор
    # (приветствие + прощание/спасибо) ≈ 78, чтобы не сливался с пустыми 60.
    if track_greeting and greeting:   score += 10
    if track_goodbye and farewell:    score += 8
    if track_upsell and upsell:       score += 8     # «ещё круче», не обязанность
    if resolved:                      score += 8

    # ── Тон голоса ───────────────────────────────────────────
    if tone == "positive":            score += 6
    elif tone == "negative":          score -= 12

    # ── Грубость — один штраф ────────────────────────────────
    if rudeness:                      score -= 30

    # ── Удовлетворённость клиента ────────────────────────────
    if customer_satisfaction is not None:
        score += {1: -15, 2: -8, 3: 0, 4: 4, 5: 8}.get(int(customer_satisfaction), 0)

    # ── Вовлечённость кассира (energy_level) ─────────────────
    if energy_level is not None:
        score += {1: -6, 2: -3, 3: 0, 4: 2, 5: 4}.get(int(energy_level), 0)

    # ── Косвенное подозрение на фрод — штраф без обнуления ───
    if fraud and fraud_confidence >= FRAUD_SOFT_THRESHOLD:
        score -= 25

    # ── Короткий / тихий визит: нейтрально, не загоняем в минус ──
    if is_short and not rudeness and not fraud:
        score = max(score, 55.0)

    return int(max(0, min(100, round(score))))
