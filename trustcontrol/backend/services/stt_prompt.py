# ════════════════════════════════════════════════════════════
#  Сборка промпта для STT (gpt-4o-transcribe / whisper-1)
#
#  Единственное место где формируется prompt параметр для
#  Whisper-совместимых моделей. Два уровня глоссария:
#    1. Базовый — платёжная лексика + казахские вставки (все точки)
#    2. Точки   — custom_phrases + names/variants из menu_json
#
#  Предупреждает если промпт > 200 слов: длинный prompt вызывает
#  галлюцинации в gpt-4o-transcribe / whisper-1.
# ════════════════════════════════════════════════════════════

import logging
from typing import Any

log = logging.getLogger("stt_prompt")

# ── Базовый глоссарий (уровень 1) ────────────────────────────
# Слова, наиболее часто искажаемые STT на казахских кассах.
# Платёжная лексика особенно важна: ошибки здесь → пропущенный фрод.
_BASE_GLOSSARY: list[str] = [
    # Платёжные слова (критично для антифрода)
    "Каспи", "Kaspi", "KaspiQR", "Халык", "Halyk",
    "QR", "терминал", "наличные", "налом",
    "карта", "картой", "картамен", "чек", "сдача",
    "перевод", "аударыңыз", "аудар", "төлеу", "төле",
    # Казахские вставки (шала-казахский / code-switching)
    "сәлем", "салам", "рахмет", "рақмет",
    "ия", "жоқ", "болды", "болады", "жақсы",
    "сау болыңыз", "не аласыз", "не берейін", "тағы не",
    # Числа / деньги
    "теңге", "тенге", "мың", "жүз", "елу",
    "отыз", "жиырма", "он",
    "бір", "екі", "үш", "төрт", "бес",
    # Размеры (кафе / фастфуд / магазин)
    "S", "M", "L", "XL",
    "маленький", "средний", "большой",
    "кіші", "орта", "үлкен",
]

# Базовая инструкция (из прежнего хардкода в _transcribe_audio):
# code-switching — норма, не переводить, транскрибировать всех.
_BASE_INSTRUCTION = (
    "Запись разговора с кассы в Казахстане. "
    "Транскрибируй ВСЕ голоса: кассира (близко к микрофону) И клиента "
    "(стоит дальше, голос тише — это живой человек, не фон). "
    "Шала-казахский (смесь русского и казахского) — НОРМА, "
    "пиши каждое слово на языке оригинала, не переводи."
)

_WARN_WORDS = 200  # Whisper prompt ≈224 токена; выше — риск галлюцинаций


def flatten_menu_glossary(menu_json: Any) -> list[str]:
    """
    Извлекает плоский список слов из menu_json для промпта транскрипции.
    Берёт name + все variants каждой позиции. price игнорируется (не нужен STT).

    Пример:
      [{"name": "Капучино", "variants": ["S","M","L"], "price": 800}]
      → ["Капучино", "S", "M", "L"]
    """
    if not menu_json or not isinstance(menu_json, list):
        return []
    words: list[str] = []
    for item in menu_json:
        if not isinstance(item, dict):
            continue
        name = (item.get("name") or "").strip()
        if name:
            words.append(name)
        for v in item.get("variants") or []:
            v = str(v).strip()
            if v:
                words.append(v)
    return words


def build_transcription_prompt(location_glossary: list[str] | None = None) -> str:
    """
    Единая точка сборки STT prompt.

    location_glossary — плоский список слов точки:
      custom_phrases (заполненные владельцем) +
      flatten_menu_glossary(location.menu_json).
    Если None/пустой — используется только базовый глоссарий.

    Дедуплицирует: слова уже присутствующие в базовом глоссарии не дублируются.
    """
    parts = [
        _BASE_INSTRUCTION,
        "Опорные слова: " + ", ".join(_BASE_GLOSSARY) + ".",
    ]

    if location_glossary:
        base_lower = {w.lower() for w in _BASE_GLOSSARY}
        seen: set[str] = set()
        extras: list[str] = []
        for w in location_glossary:
            w = w.strip()
            if not w:
                continue
            wl = w.lower()
            if wl in base_lower or wl in seen:
                continue
            seen.add(wl)
            extras.append(w)
        if extras:
            parts.append("Слова заведения: " + ", ".join(extras) + ".")

    prompt = " ".join(parts)

    word_count = len(prompt.split())
    if word_count > _WARN_WORDS:
        log.warning(
            f"STT prompt: {word_count} слов > {_WARN_WORDS} — "
            "длинный глоссарий может вызвать галлюцинации. "
            "Сократите custom_phrases точки."
        )
    else:
        log.debug(f"STT prompt: {word_count} слов")

    return prompt
