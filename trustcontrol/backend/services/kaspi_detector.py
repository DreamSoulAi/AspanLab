# ════════════════════════════════════════════════════════════
#  Сервис: Kaspi Antifraud Detector
#
#  Логика:
#  1. Проверяем есть ли в транскрипте контекст Каспи-перевода
#     (слова "перевод", "на Каспи", "на этот номер" и т.д.)
#  2. Вытаскиваем все номера телефонов (RU/KZ форматы)
#  3. Сверяем с белым списком из location.allowed_phones
#  4. Если номер не в списке → возвращаем подозрительный инцидент
#
#  Форматы номеров в Казахстане:
#    +7 (707) 123-45-67, +77071234567, 87071234567,
#    77071234567, 70XXXXXXXXX
# ════════════════════════════════════════════════════════════

import re
import logging

log = logging.getLogger("kaspi_detector")

# Фразы, которые указывают на Каспи-перевод в разговоре
_TRIGGER_PHRASES = [
    "на каспи", "в каспи", "каспи голд", "каспи ред", "kaspi",
    "переведи", "перевод", "переводи", "перекинь", "скинь",
    "на этот номер", "на номер", "отправь на",
    "скинуть", "перекидывай",
    # Казахские
    "аударыңыз", "аудар", "жібер", "жіберіңіз", "нөмірге",
    "қаспи", "каспий",
]

# Паттерны телефонных номеров в Казахстане
_PHONE_RE = re.compile(
    r"(?:"
    r"\+7[\s\-]?\(?\d{3}\)?[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}"  # +7 (707) 123-45-67
    r"|"
    r"\+77\d{9}"                                                      # +77071234567
    r"|"
    r"8[\s\-]?\(?\d{3}\)?[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}"    # 8 (707) 123-45-67
    r"|"
    r"87\d{9}"                                                        # 87071234567
    r"|"
    r"77\d{9}"                                                        # 77071234567
    r"|"
    r"70\d{9}"                                                        # 70XXXXXXXXX
    r")"
)


def normalize_phone(raw: str) -> str:
    """Нормализует любой формат к +7XXXXXXXXXX."""
    digits = re.sub(r"[^\d]", "", raw)
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    if len(digits) == 10:
        digits = "7" + digits
    return "+" + digits if not digits.startswith("+") else digits


def has_kaspi_context(text: str) -> bool:
    """True если текст содержит контекст Каспи-перевода."""
    tl = text.lower()
    return any(phrase in tl for phrase in _TRIGGER_PHRASES)


def extract_phones(text: str) -> list[str]:
    """Возвращает список нормализованных номеров из текста (без дублей)."""
    seen = []
    for m in _PHONE_RE.finditer(text):
        normalized = normalize_phone(m.group(0))
        if normalized not in seen:
            seen.append(normalized)
    return seen


def check_kaspi_fraud(
    transcript: str,
    allowed_phones: list[str],
    payment_mode: str = "mixed",
) -> list[dict]:
    """
    Основная проверка.

    Возвращает список подозрительных номеров:
      [{"phone": "+77071234567", "normalized": "+77071234567", "confidence": "high"|"low"}]
    Пустой список — всё чисто.

    confidence="high" → KASPI_FRAUD (severity critical)
    confidence="low"  → KASPI_UNVERIFIED (severity warning, белый список не настроен)

    ВОРОТА: ТОЛЬКО has_transfer_intent() во ВСЕХ режимах.
    Голое "каспи"/"kaspi" без намерения перевода — ворота закрыты всегда.

    Алгоритм по payment_mode:
      qr_only/cash_only  → intent + номер = high всегда (переводы недопустимы)
      transfers_ok/mixed → intent + номер не в белом списке = high
                           intent + пустой белый список = low (не обвиняем)
    """
    # Ворота: только узкий intent, НЕ широкое "каспи"
    if not has_transfer_intent(transcript):
        return []

    phones = extract_phones(transcript)
    if not phones:
        return []

    # qr_only/cash_only: переводы режимом не предусмотрены — любой номер fraud
    if payment_mode in ("qr_only", "cash_only"):
        for p in phones:
            log.warning(f"Kaspi fraud: {p} на точке режима {payment_mode} (переводы недопустимы)")
        return [{"phone": p, "normalized": p, "confidence": "high"} for p in phones]

    # transfers_ok/mixed: сверяем с белым списком
    allowed_normalized = {normalize_phone(p) for p in (allowed_phones or [])}

    if not allowed_normalized:
        # Пустой белый список — мягкий путь, не обвиняем кассира жёстко
        log.info(f"Kaspi intent, белый список пуст → soft flag (low) для {phones}")
        return [{"phone": p, "normalized": p, "confidence": "low"} for p in phones]

    suspicious = []
    for phone in phones:
        if phone not in allowed_normalized:
            log.warning(f"Kaspi fraud: {phone} не в белом списке (в списке: {len(allowed_normalized)})")
            suspicious.append({"phone": phone, "normalized": phone, "confidence": "high"})

    return suspicious
