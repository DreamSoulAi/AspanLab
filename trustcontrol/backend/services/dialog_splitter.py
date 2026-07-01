# ════════════════════════════════════════════════════════════
#  Сервис: Умное разбиение записи на отдельные диалоги (Слой 2)
#
#  Зачем: одна запись с кассы (один submit, до 5 минут) может содержать
#  НЕСКОЛЬКО клиентов подряд + болтовню персонала. Акустически на кассе их
#  не разделить (очередь без пауз), поэтому режем уже ГОТОВЫЙ транскрипт —
#  поверх текста, с учётом профиля точки.
#
#  Логика: один дешёвый вызов gpt-4o-mini читает весь транскрипт и возвращает
#  список диалогов с типом SERVICE / PERSONAL / UNCLEAR.
#    SERVICE  — диалог кассир↔клиент → отдельный Report + анализ + фрод.
#    PERSONAL — болтовня персонала   → пропускаем (не платим за анализ).
#    UNCLEAR  — граница неочевидна   → анализируем на всякий случай (фрод!).
#
#  Безопасность (НЕ ломать текущий путь):
#    • Любая ошибка / пустой ответ / невалидный JSON → возвращаем []
#      (вызывающий код анализирует весь транскрипт как сейчас — фолбэк).
#    • При сомнении модель обязана вернуть ОДИН кусок UNCLEAR, а не рвать
#      на неправильные границы. Один кусок лучше неверного разрыва.
# ════════════════════════════════════════════════════════════

import json
import logging

from backend.config import settings
from backend.services.gpt_analyzer import text_client as client, _TEXT_MODEL

log = logging.getLogger("dialog_splitter")

# Ниже этого числа слов резать смысла нет — это заведомо один короткий диалог.
# Экономит вызов gpt-4o-mini на типовой быстрой сделке ("Кофе. 800. QR").
_MIN_WORDS_TO_SPLIT = 25

_VALID_TYPES = {"SERVICE", "PERSONAL", "UNCLEAR"}

_TYPE_NAMES = {
    "coffee": "кофейня", "cafe": "кафе/ресторан", "fastfood": "фастфуд",
    "gas": "АЗС/заправка", "shop": "магазин/розница", "beauty": "салон красоты",
    "fitness": "фитнес-клуб", "hotel": "отель/гостиница",
    "pharmacy": "аптека", "clinic": "клиника/медцентр",
    "auto": "автосервис/автомойка", "service": "сфера услуг", "other": "бизнес",
}

_PAYMENT_HINTS = {
    "qr_only":      "оплата ТОЛЬКО по QR (переводов на номер быть не должно)",
    "cash_only":    "оплата ТОЛЬКО наличными (переводов на номер быть не должно)",
    "transfers_ok": "принимают переводы на номер Каспи (это норма)",
    "mixed":        "оплата разная: QR, наличные, иногда перевод",
}


def build_prompt(
    transcript: str,
    business_type: str = None,
    payment_mode: str = "mixed",
    greeting_script: str = None,
) -> str:
    """Собирает промпт для gpt-4o-mini. Профиль точки → в инструкцию (маркеры)."""
    biz = _TYPE_NAMES.get(business_type or "", business_type or "торговая точка")
    pay = _PAYMENT_HINTS.get(payment_mode or "mixed", _PAYMENT_HINTS["mixed"])
    greet = (greeting_script or "").strip()
    greet_line = f"- Приветствие этой точки (ориентир): «{greet[:200]}»\n" if greet else ""

    return (
        "Ты разбиваешь транскрипт аудиозаписи кассы на ОТДЕЛЬНЫЕ диалоги.\n"
        "Запись могла захватить несколько клиентов подряд и болтовню персонала.\n\n"
        f"ПРОФИЛЬ ТОЧКИ:\n- Тип бизнеса: {biz}\n- Режим оплаты: {pay}\n{greet_line}\n"
        "ПРАВИЛА (работай на русском И казахском, маркеры на обоих языках):\n"
        "1. SERVICE — один диалог кассир↔клиент.\n"
        "   НАЧАЛО: приветствие/обращение к клиенту («здравствуйте», «сәлем»,\n"
        "   «добрый день», «қайырлы күн», «слушаю», «что будете», «не аласыз»,\n"
        "   «следующий»), либо явно новый голос клиента с заказом.\n"
        "   КОНЕЦ: оплата (звучит СУММА + способ: QR/наличные/карта/перевод)\n"
        "   ИЛИ прощание («спасибо», «рахмет», «до свидания», «сау болыңыз»),\n"
        "   ИЛИ метка [тишина]/[пауза] в тексте.\n"
        "2. PERSONAL — персонал говорит между собой: НЕТ приветствия клиента,\n"
        "   НЕТ заказа, НЕТ суммы оплаты. Болтовня, личное, кухонные команды.\n"
        "3. UNCLEAR — граница неочевидна (очередь без пауз, обрывок).\n"
        "   ВАЖНО: при сомнении верни ОДИН кусок UNCLEAR — это лучше, чем\n"
        "   неправильно разорвать на два SERVICE. Не выдумывай границы.\n"
        "4. Заказ ВНУТРИ болтовни — это всё равно SERVICE (не теряй сделку).\n\n"
        "Верни ТОЛЬКО JSON-объект без пояснений, в формате:\n"
        '{"dialogues": [{"text": "<точный текст сегмента из транскрипта>", '
        '"type": "SERVICE|PERSONAL|UNCLEAR", '
        '"start_marker": "<фраза-начало или null>", '
        '"end_marker": "<фраза-конец или null>"}]}\n'
        "Текст сегментов в сумме должен покрывать весь транскрипт без выдумок.\n\n"
        f"━━━ ТРАНСКРИПТ ━━━\n{transcript}"
    )


def _has_service_markers(seg: dict) -> bool:
    return bool(seg.get("start_marker") or seg.get("end_marker"))


async def split_into_dialogues(
    transcript: str,
    business_type: str = None,
    payment_mode: str = "mixed",
    greeting_script: str = None,
) -> list[dict]:
    """
    Режет транскрипт на отдельные диалоги через gpt-4o-mini.

    Возвращает список:
      [{"text", "type": "SERVICE|PERSONAL|UNCLEAR",
        "start_marker", "end_marker", "has_service_markers"}]

    Пустой список [] — НЕ резали (короткий текст / ошибка / один диалог).
    Вызывающий код в этом случае анализирует весь транскрипт как сейчас.
    """
    text = (transcript or "").strip()
    if not settings.OPENAI_API_KEY:
        return []
    if len(text.split()) < _MIN_WORDS_TO_SPLIT:
        # Короткая запись — заведомо один диалог, не платим за разбиение.
        return []

    try:
        prompt = build_prompt(text, business_type, payment_mode, greeting_script)
        resp = await client.chat.completions.create(
            model=_TEXT_MODEL,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            temperature=0,
            max_tokens=2000,
        )
        raw = (resp.choices[0].message.content or "").strip()
        data = json.loads(raw)
    except Exception as e:
        log.warning(f"split_into_dialogues: ошибка модели/JSON ({e}) — фолбэк на цельный анализ")
        return []

    dialogues = data.get("dialogues") if isinstance(data, dict) else None
    if not isinstance(dialogues, list) or not dialogues:
        return []

    out: list[dict] = []
    for seg in dialogues:
        if not isinstance(seg, dict):
            continue
        seg_text = (seg.get("text") or "").strip()
        if not seg_text:
            continue
        seg_type = (seg.get("type") or "UNCLEAR").upper()
        if seg_type not in _VALID_TYPES:
            seg_type = "UNCLEAR"
        out.append({
            "text":                seg_text,
            "type":                seg_type,
            "start_marker":        seg.get("start_marker"),
            "end_marker":          seg.get("end_marker"),
            "has_service_markers": _has_service_markers(seg),
        })

    # Один сегмент → резать было нечего, пусть идёт цельным путём (фолбэк).
    if len(out) < 2:
        return []

    log.info(
        f"split_into_dialogues: {len(out)} сегментов "
        f"({sum(1 for s in out if s['type']=='SERVICE')} SERVICE / "
        f"{sum(1 for s in out if s['type']=='PERSONAL')} PERSONAL / "
        f"{sum(1 for s in out if s['type']=='UNCLEAR')} UNCLEAR)"
    )
    return out
