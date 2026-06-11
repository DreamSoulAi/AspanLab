# ════════════════════════════════════════════════════════════
#  Сервис: распознавание меню с фото через gpt-4o-mini vision
#
#  Вызывается ТОЛЬКО вручную владельцем при загрузке фото меню
#  (не на каждую запись) — никакого влияния на стоимость анализа.
#  Возвращает структуру меню: name + variants(размеры) + price.
#  Результат показывается владельцу в редактируемом виде, не сохраняется
#  напрямую — владелец правит и подтверждает.
# ════════════════════════════════════════════════════════════

import base64
import json
import logging

from openai import AsyncOpenAI
from backend.config import settings

log = logging.getLogger("menu_vision")
client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY, timeout=60.0, max_retries=1)

_VISION_MODEL = "gpt-4o-mini"   # поддерживает vision, дёшево

_MENU_PROMPT = """На фото — меню заведения (кафе/кофейня/магазин/фастфуд) в Казахстане.
Извлеки структуру меню в JSON. Для каждой позиции:
- name: название КАК В МЕНЮ, на языке оригинала (казахский/русский). НЕ переводи.
- variants: список размеров/вариантов РОВНО как в меню — «S/M/L», «маленький/большой»,
  «0.3/0.5», «одинарный/двойной». Если вариантов нет — пустой список [].
- price: цена числом (за базовый/первый вариант). Если цена не видна — null.

Правила:
- НЕ выдумывай позиции которых нет на фото.
- Размеры бери ровно как написаны (не нормализуй S→маленький).
- Если на фото несколько колонок/страниц — собери все позиции.

Верни ТОЛЬКО валидный JSON:
{"items": [{"name": "Капучино", "variants": ["S", "M", "L"], "price": 800}]}"""


def _img_mime(data: bytes) -> str:
    """Определяет MIME по сигнатуре. OpenAI принимает jpeg/png/webp/gif."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"GIF":
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def _normalize_items(items) -> list[dict]:
    """Чистит и ограничивает распознанное меню: name + variants[] + price|null."""
    if not isinstance(items, list):
        return []
    out: list[dict] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        name = (it.get("name") or "").strip()
        if not name:
            continue
        variants = [str(v).strip() for v in (it.get("variants") or []) if str(v).strip()]
        price = it.get("price")
        try:
            price = int(price) if price is not None else None
        except (TypeError, ValueError):
            price = None
        out.append({"name": name[:80], "variants": variants[:10], "price": price})
    return out[:150]


async def extract_menu_from_images(images: list[bytes]) -> list[dict]:
    """
    Распознаёт меню с 1-3 фото через gpt-4o-mini vision.
    Возвращает список позиций [{name, variants, price}].
    Бросает RuntimeError при отсутствии ключа или ошибке распознавания —
    вызывающий endpoint превращает это во внятное сообщение пользователю.
    """
    if not settings.OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY не задан")
    if not images:
        return []

    content: list = [{"type": "text", "text": _MENU_PROMPT}]
    for img in images:
        b64 = base64.b64encode(img).decode()
        mime = _img_mime(img)
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:{mime};base64,{b64}"},
        })

    resp = await client.chat.completions.create(
        model=_VISION_MODEL,
        messages=[{"role": "user", "content": content}],
        response_format={"type": "json_object"},
        max_tokens=2500,
        temperature=0.1,
    )
    raw = (resp.choices[0].message.content or "").strip()
    data = json.loads(raw)   # JSON-mode гарантирует валидный JSON
    items = data.get("items") if isinstance(data, dict) else data
    result = _normalize_items(items)
    log.info(f"Распознано меню: {len(result)} позиций с {len(images)} фото")
    return result
