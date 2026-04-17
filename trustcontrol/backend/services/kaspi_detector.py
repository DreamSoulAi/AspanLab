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
) -> list[dict]:
    """
    Основная проверка.

    Возвращает список подозрительных номеров:
      [{"phone": "+77071234567", "normalized": "+77071234567"}]
    Пустой список — всё чисто.

    Алгоритм:
      1. Есть ли Каспи-контекст? Нет — выходим (false-positive protection)
      2. Вытаскиваем номера из транскрипта
      3. Сверяем с белым списком владельца
      4. Возвращаем только те что НЕ в белом списке
    """
    if not has_kaspi_context(transcript):
        return []

    phones = extract_phones(transcript)
    if not phones:
        return []

    allowed_normalized = {normalize_phone(p) for p in (allowed_phones or [])}

    suspicious = []
    for phone in phones:
        if phone not in allowed_normalized:
            log.warning(f"Kaspi fraud: {phone} не в белом списке (всего в списке: {len(allowed_normalized)})")
            suspicious.append({"phone": phone, "normalized": phone})

    return suspicious
