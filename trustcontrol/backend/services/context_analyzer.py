# ════════════════════════════════════════════════════════════
#  Сервис: Contextual Severity — Детектор Клиента
#
#  Определяет контекст разговора прежде чем бросить тревогу.
#  Три слоя анализа:
#    1. POS-синхронизация — есть ли открытый чек? (главный сигнал)
#    2. Маркеры обслуживания — «Вам с собой?», «Оплата картой?»...
#    3. Диаризация — сколько голосов, есть ли «незнакомый» голос?
#
#  Результат:
#    "customer_service" → AGGRESSION/FRAUD = критично, слать алерт
#    "internal_talk"    → мат/конфликт = сотрудники между собой, логируем тихо
#    "unknown"          → среднее (medium priority)
# ════════════════════════════════════════════════════════════

import re
import logging

log = logging.getLogger("context_analyzer")

# ── Маркеры обслуживания клиента ─────────────────────────────────────────────
# Фразы, которые однозначно указывают что рядом клиент и идёт обслуживание.

_SERVICE_MARKERS_RU = [
    # Приветствие клиента
    "добрый день", "добрый вечер", "доброе утро", "здравствуйте", "добро пожаловать",
    "рады вас видеть", "чем могу помочь", "чем могу",

    # Вопросы заказа
    "что будете", "что желаете", "что закажете", "что вам", "что для вас",
    "ваш заказ", "вам с собой", "здесь будете", "на месте", "на вынос",
    "вам одно", "вам два", "вам три",

    # Продукт / напиток
    "ваш кофе", "ваш напиток", "ваш заказ готов", "ваш чай", "ваша",
    "рекомендую", "у нас есть", "сегодня акция", "хотите попробовать",

    # Оплата
    "оплата картой", "оплатите", "наличными", "с вас", "итого",
    "чек нужен", "чек выдать", "чек", "сдача", "сдачу",
    "приложите карту", "введите пин", "оплата прошла", "спасибо за покупку",

    # Прощание с клиентом
    "хорошего дня", "приятного аппетита", "приходите ещё",
    "до свидания", "всего доброго", "удачи",

    # Допродажа
    "хотите сироп", "хотите десерт", "добавить", "к кофе",
]

_SERVICE_MARKERS_KK = [
    # Казахские
    "қайырлы күн", "сәлеметсіз бе", "қош келдіңіз",
    "сізге не керек", "тағы не керек", "не аласыз",
    "картамен", "қолма-қол", "төлейміз", "чек керек",
    "рахмет", "сау болыңыз", "қош болыңыз",
    "сізге сироп", "қосамыз ба", "алымыз ба",
]

_ALL_SERVICE_MARKERS = _SERVICE_MARKERS_RU + _SERVICE_MARKERS_KK


def count_service_markers(text: str) -> int:
    """Подсчитывает количество маркеров обслуживания в тексте."""
    tl = text.lower()
    return sum(1 for marker in _ALL_SERVICE_MARKERS if marker in tl)


def has_payment_talk(text: str) -> bool:
    """True если в тексте есть явное обсуждение оплаты."""
    tl = text.lower()
    payment_words = [
        "оплат", "наличн", "картой", "перевод", "каспи",
        "чек", "сдач", "итого", "с вас",
        "төлейміз", "картамен", "қолма-қол",
    ]
    return any(w in tl for w in payment_words)


def analyze_context(
    transcript: str,
    events: dict,
    speakers: list,
    has_pos_nearby: bool,
    customer_satisfaction: int | None = None,
    is_personal_talk: bool = False,
) -> dict:
    """
    Определяет контекст разговора по трём слоям сигналов.

    Возвращает dict:
      context    : "customer_service" | "internal_talk" | "unknown"
      confidence : 0.0 – 1.0
      score      : float (положительный = клиент рядом, отрицательный = внутренний)
      has_service_markers : bool
      has_pos_nearby      : bool
      speaker_count       : int
      reason     : str (для лога)
    """
    # Если GPT уже пометил как личный разговор — сразу internal
    if is_personal_talk:
        return _result("internal_talk", 0.95, -0.95,
                       False, has_pos_nearby, len(speakers) if speakers else 0,
                       "GPT: is_personal_talk=True")

    score   = 0.0
    reasons = []

    # ── Слой 1: POS-синхронизация (главный сигнал) ──────────────────────────
    if has_pos_nearby:
        score += 0.90
        reasons.append("POS-транзакция ±3 мин")

    # ── Слой 2: Маркеры обслуживания ────────────────────────────────────────
    service_count = count_service_markers(transcript)
    if service_count >= 3:
        score += 0.55
        reasons.append(f"{service_count} маркеров сервиса")
    elif service_count >= 1:
        score += 0.25
        reasons.append(f"{service_count} маркер сервиса")

    if has_payment_talk(transcript):
        score += 0.30
        reasons.append("обсуждение оплаты")

    # ── GPT events (дополнительные сигналы) ─────────────────────────────────
    if events.get("greeting"):
        score += 0.25
        reasons.append("GPT: приветствие")
    if events.get("farewell"):
        score += 0.15
        reasons.append("GPT: прощание")
    if events.get("issue_resolved"):
        score += 0.15
        reasons.append("GPT: вопрос решён")

    # ── Слой 3: Диаризация (количество голосов) ─────────────────────────────
    speaker_count = len(speakers) if speakers else 0
    if speaker_count > 2:
        score += 0.40
        reasons.append(f"{speaker_count} голоса — вероятно клиент")
    elif speaker_count == 2:
        # Два голоса — может быть и кассир+клиент и два сотрудника
        # Роли из диаризации помогут
        roles = {s.get("role", "") for s in (speakers or [])}
        if "customer" in roles:
            score += 0.60
            reasons.append("диаризация: роль 'customer' найдена")

    # ── GPT customer_satisfaction (клиент оценён → был рядом) ───────────────
    if customer_satisfaction and customer_satisfaction > 0:
        score += 0.35
        reasons.append("GPT: оценка удовлетворённости")

    # ── Антисигналы (признаки внутреннего разговора) ────────────────────────
    if not has_pos_nearby and service_count == 0 and not events.get("greeting"):
        score -= 0.45
        reasons.append("нет POS, нет маркеров, нет приветствия")

    if not has_pos_nearby and not has_payment_talk(transcript) and speaker_count <= 2:
        score -= 0.20
        reasons.append("нет оплаты, 2 голоса")

    # ── Итог ────────────────────────────────────────────────────────────────
    if score >= 0.45:
        context = "customer_service"
    elif score <= -0.30:
        context = "internal_talk"
    else:
        context = "unknown"

    confidence = min(1.0, abs(score))
    has_markers = service_count > 0

    log.debug(
        f"context={context} score={score:.2f} "
        f"POS={has_pos_nearby} markers={service_count} "
        f"speakers={speaker_count} | {' / '.join(reasons) or '—'}"
    )

    return _result(context, confidence, score, has_markers, has_pos_nearby, speaker_count,
                   " / ".join(reasons) or "недостаточно данных")


def _result(context, confidence, score, has_markers, has_pos, speakers, reason):
    return {
        "context":             context,
        "confidence":          round(confidence, 2),
        "score":               round(score, 2),
        "has_service_markers": has_markers,
        "has_pos_nearby":      has_pos,
        "speaker_count":       speakers,
        "reason":              reason,
    }


async def check_pos_window(location_id: int, ts, db) -> bool:
    """
    Проверяет есть ли POS-транзакция в ±3 мин от timestamp ts.
    Используется как вход для analyze_context().
    """
    from datetime import timedelta
    from sqlalchemy import select
    from backend.models.pos_transaction import PosTransaction

    window_start = ts - timedelta(minutes=3)
    window_end   = ts + timedelta(minutes=3)
    result = await db.execute(
        select(PosTransaction.id).where(
            PosTransaction.location_id == location_id,
            PosTransaction.timestamp   >= window_start,
            PosTransaction.timestamp   <= window_end,
        ).limit(1)
    )
    return result.scalar() is not None
