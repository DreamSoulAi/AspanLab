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

# gpt-4o (полная) — vision-OCR плотных меню с мелкими цифрами.
# Вызов РУЧНОЙ, один раз на точку → цена (~2-3₸ за фото) не важна,
# а mini проваливал: читал 7 позиций из 30 и путал цифры (1200→1230).
_VISION_MODEL = "gpt-4o"

_MENU_PROMPT = """На фото — меню заведения (кафе/кофейня/магазин/фастфуд) в Казахстане.
Твоя задача — извлечь ВСЁ меню целиком в JSON, ничего не пропустив.

ГЛАВНОЕ ПРАВИЛО — ПОЛНОТА:
Прочитай КАЖДУЮ строку в КАЖДОМ разделе (Донер, Комбо, Кебав, Чикен, Фри, Напитки и т.д.).
В типичном меню 20-40 позиций. Если извлёк меньше 15 — ты что-то пропустил, перечитай фото.
Иди раздел за разделом сверху вниз, слева направо, не перескакивай.

ДВЕ ЦЕНЫ В ОДНОЙ СТРОКЕ (частый случай в КЗ):
Если у позиции в строке стоят ДВЕ (или больше) цены — это РАЗНЫЕ РАЗМЕРЫ одной позиции,
а НЕ две отдельные позиции и НЕ одна усреднённая цена.
- Если над колонками есть заголовки размеров (обычный/большой, S/M, 0.5/1л) — бери их.
- Если заголовков нет — назови размеры по смыслу: первая (меньшая) цена = «обычный»,
  вторая (большая) = «большой». Третья — «XL».
- Запиши размеры в variants, цены — в prices (по порядку, тех же размеров).
Пример: «Донер 1200 / 1600» → variants ["обычный","большой"], prices [1200,1600], price 1200.

ТОЧНОСТЬ ЦИФР:
Переписывай цены СИМВОЛ В СИМВОЛ как на фото. НЕ округляй, НЕ додумывай.
Если цифра неразборчива — лучше null чем угаданное число.

Для каждой позиции:
- name: название КАК В МЕНЮ, на языке оригинала (казахский/русский). НЕ переводи.
- variants: список размеров (см. выше). Если размер один — пустой список [].
- prices: список цен по порядку размеров. Если размер один — [одна_цена].
- price: первая (базовая) цена числом. Если цена не видна — null.

НЕ выдумывай позиции которых нет на фото. Размеры пиши как в меню (не нормализуй S→маленький).

Верни ТОЛЬКО валидный JSON:
{"items": [{"name": "Донер", "variants": ["обычный","большой"], "prices": [1200,1600], "price": 1200}]}"""


def _img_mime(data: bytes) -> str:
    """Определяет MIME по сигнатуре. OpenAI принимает jpeg/png/webp/gif."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"GIF":
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def _to_int_price(v):
    """Парсит цену в int, выкидывая пробелы/₸/тг. None если не число."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return int(v)
    digits = "".join(ch for ch in str(v) if ch.isdigit())
    return int(digits) if digits else None


def _normalize_items(items) -> list[dict]:
    """Чистит распознанное меню: name + variants[] + prices[] + price|null."""
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
        prices = [p for p in (_to_int_price(x) for x in (it.get("prices") or [])) if p is not None]
        # price = базовая цена: явная, либо первая из prices
        price = _to_int_price(it.get("price"))
        if price is None and prices:
            price = prices[0]
        out.append({
            "name":     name[:80],
            "variants": variants[:10],
            "prices":   prices[:10],
            "price":    price,
        })
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
        max_tokens=4096,
        temperature=0.1,
    )
    raw = (resp.choices[0].message.content or "").strip()
    data = json.loads(raw)   # JSON-mode гарантирует валидный JSON
    items = data.get("items") if isinstance(data, dict) else data
    result = _normalize_items(items)
    log.info(f"Распознано меню: {len(result)} позиций с {len(images)} фото")
    return result
