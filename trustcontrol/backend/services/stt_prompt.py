# ════════════════════════════════════════════════════════════
#  Сборка промпта для STT (gpt-4o-transcribe / whisper-1)
#
#  Единственное место где формируется prompt параметр для
#  Whisper-совместимых моделей. Два уровня:
#    1. Базовый — CRITICAL + STANDARD (общий для всех точек)
#    2. Точки   — custom_phrases + names/variants из menu_json
#
#  Жёсткий лимит 180 слов: глоссарий точки обрезается если
#  не влезает, CRITICAL никогда не режется.
# ════════════════════════════════════════════════════════════

import logging
from typing import Any

log = logging.getLogger("stt_prompt")

# ── CRITICAL — платёжные / фрод-термины (никогда не урезаем) ─
# Если STT исказит эти слова → фрод не детектируется.
# Убраны дубли: карта (→ картамен), налом (→ наличные),
# Halyk (менее критично чем Халык).
_CRITICAL: list[str] = [
    "Каспи", "Kaspi", "KaspiQR", "Халык",
    "QR", "терминал", "наличные",
    "картой", "картамен", "чек", "сдача",
    "перевод", "аударыңыз", "аудар", "төлеу", "төле",
]

# ── STANDARD — деньги + казахские вставки ──────────────────
# Нужны для корректного восприятия шала-казахского контекста.
# Числа (бір-бес, елу, отыз...) убраны — Whisper их знает сам.
# Размеры (S/M/L, маленький...) убраны — идут из menu_json точки.
_STANDARD: list[str] = [
    "теңге", "мың", "жүз",
    "сәлем", "рахмет", "ия", "жоқ", "болды", "жақсы",
    "сау болыңыз",
]

_BASE_GLOSSARY: list[str] = _CRITICAL + _STANDARD  # CRITICAL стоит первым

_BASE_INSTRUCTION = (
    "Запись разговора с кассы в Казахстане. "
    "Транскрибируй ВСЕ голоса: кассира (близко к микрофону) И клиента "
    "(стоит дальше, голос тише — это живой человек, не фон). "
    "Шала-казахский (смесь русского и казахского) — НОРМА, "
    "пиши каждое слово на языке оригинала, не переводи. "
    "Пиши ТОЛЬКО кириллицей (русский и казахский алфавит), НИКОГДА не латиницей. "
    "Если диктуют номер телефона или счёта (для оплаты/перевода) — "
    "запиши его цифрами подряд, максимально точно по услышанным цифрам "
    "(казахский или русский счёт)."
)

# Whisper prompt ≈224 токена. Держим запас — после 180 слов растёт
# риск галлюцинаций (модель «дополняет» глоссарий).
_MAX_TOTAL_WORDS = 180


def flatten_menu_glossary(menu_json: Any) -> list[str]:
    """
    Извлекает плоский список слов из menu_json для промпта транскрипции.
    Берёт name + все variants каждой позиции. price игнорируется.

    Пример:
      [{"name": "Капучино", "variants": ["S", "M", "L"], "price": 800}]
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

    Уровень 1 — базовый глоссарий (CRITICAL + STANDARD):
      ~26 элементов ≈ 67 слов в промпте.
      CRITICAL (платёж/фрод) никогда не урезается.

    Уровень 2 — глоссарий точки:
      custom_phrases + flatten_menu_glossary(menu_json).
      Дедуплицируется относительно базы. Обрезается по словам
      если суммарный промпт превысит _MAX_TOTAL_WORDS (180).
      Логирует сколько слов отброшено.
    """
    base_lower = {w.lower() for w in _BASE_GLOSSARY}

    base_parts = [
        _BASE_INSTRUCTION,
        "Опорные слова: " + ", ".join(_BASE_GLOSSARY) + ".",
    ]
    base_words = len(" ".join(base_parts).split())

    extra_part = ""
    if location_glossary:
        seen: set[str] = set()
        deduped: list[str] = []
        for w in location_glossary:
            w = w.strip()
            if not w:
                continue
            wl = w.lower()
            if wl in base_lower or wl in seen:
                continue
            seen.add(wl)
            deduped.append(w)

        if deduped:
            # «Слова заведения: » + «.» стоят ~3 слова — закладываем в бюджет
            budget = _MAX_TOTAL_WORDS - base_words - 3
            included: list[str] = []
            used = 0
            for w in deduped:
                w_len = len(w.split())
                if used + w_len > budget:
                    break
                included.append(w)
                used += w_len

            skipped = len(deduped) - len(included)
            if skipped > 0:
                log.warning(
                    f"STT prompt: глоссарий точки обрезан — "
                    f"добавлено {len(included)}, отброшено {skipped} слов "
                    f"(лимит {_MAX_TOTAL_WORDS}). Сократите custom_phrases."
                )
            if included:
                extra_part = "Слова заведения: " + ", ".join(included) + "."

    parts = base_parts + ([extra_part] if extra_part else [])
    prompt = " ".join(parts)
    log.debug(f"STT prompt: {len(prompt.split())} слов (лимит {_MAX_TOTAL_WORDS})")
    return prompt
