#!/usr/bin/env python3
# ════════════════════════════════════════════════════════════
#  TrustControl — Парсер лидов из 2ГИС (Алматы)
#
#  Собирает бизнесы по категориям (кофейни / аптеки / фастфуд),
#  достаёт название, телефон, адрес, рейтинг, ссылку 2ГИС → CSV.
#  CSV потом скармливается whatsapp_outreach.py для рассылки.
#
#  КАК ПОЛУЧИТЬ КЛЮЧ (бесплатно):
#    1. Зайди на https://dev.2gis.ru → «Получить ключ» (Catalog API).
#    2. Бесплатного лимита хватает на тысячи запросов в день.
#    3. Вставь ключ ниже или передай через переменную окружения:
#         export DGIS_API_KEY=ваш_ключ
#
#  ЗАПУСК:
#    pip install -r requirements-marketing.txt
#    python parse_2gis.py
#    python parse_2gis.py --categories кофейня аптека фастфуд --pages 20
#    python parse_2gis.py --city Астана
#
#  РЕЗУЛЬТАТ:
#    leads.csv — таблица лидов (название, телефон, адрес, рейтинг, ссылка)
# ════════════════════════════════════════════════════════════

from __future__ import annotations

import argparse
import csv
import os
import re
import sys
import time
from pathlib import Path

import requests

# ── Настройки ────────────────────────────────────────────────
API_KEY = os.getenv("DGIS_API_KEY", "")        # ключ Catalog API из dev.2gis.ru
BASE_URL = "https://catalog.api.2gis.com/3.0/items"
PAGE_SIZE = 50                                  # максимум 2ГИС за один запрос
REQUEST_DELAY = 0.4                             # пауза между запросами (бережём лимит)
OUT_FILE = Path(__file__).parent / "leads.csv"

# Категории по умолчанию: то что просил Данил. Синонимы расширяют охват.
DEFAULT_CATEGORIES = {
    "кофейня":  ["кофейня", "кофе с собой", "кофе"],
    "аптека":   ["аптека"],
    "фастфуд":  ["фастфуд", "донер", "шаурма", "бургер", "фаст фуд"],
}

# Поля 2ГИС которые просим вернуть (контакты, координаты, рейтинг, рубрики)
FIELDS = (
    "items.point,items.contact_groups,items.rubrics,items.address,"
    "items.reviews,items.external_content,items.region_id"
)


def _normalize_phone(raw: str) -> str:
    """+7 707 123 45 67 → 77071234567 (формат для WhatsApp wa.me)."""
    digits = re.sub(r"\D", "", raw or "")
    if digits.startswith("8") and len(digits) == 11:
        digits = "7" + digits[1:]
    if len(digits) == 10:               # 7071234567 без кода страны
        digits = "7" + digits
    return digits if len(digits) == 11 and digits.startswith("7") else ""


def _extract_phones(item: dict) -> list[str]:
    """Достаёт все телефоны из contact_groups одного бизнеса."""
    phones = []
    for group in item.get("contact_groups", []) or []:
        for c in group.get("contacts", []) or []:
            if c.get("type") == "phone":
                ph = _normalize_phone(c.get("value", ""))
                if ph and ph not in phones:
                    phones.append(ph)
    return phones


def _extract_website(item: dict) -> str:
    for group in item.get("contact_groups", []) or []:
        for c in group.get("contacts", []) or []:
            if c.get("type") in ("website", "url"):
                return c.get("url") or c.get("value", "")
    return ""


def _extract_rating(item: dict) -> tuple[str, str]:
    rv = item.get("reviews") or {}
    rating = rv.get("general_rating") or rv.get("rating") or ""
    count = rv.get("general_review_count") or rv.get("review_count") or ""
    return str(rating), str(count)


def fetch_category(query: str, city: str, max_pages: int) -> list[dict]:
    """Запрашивает 2ГИС постранично по одному поисковому запросу."""
    results = []
    page = 1
    while page <= max_pages:
        params = {
            "q": f"{query} {city}",
            "fields": FIELDS,
            "page": page,
            "page_size": PAGE_SIZE,
            "key": API_KEY,
        }
        try:
            r = requests.get(BASE_URL, params=params, timeout=20)
        except Exception as e:
            print(f"   ! сеть: {e}")
            break

        if r.status_code != 200:
            print(f"   ! HTTP {r.status_code}: {r.text[:160]}")
            break

        data = r.json()
        meta = data.get("meta", {})
        if meta.get("code") and meta["code"] != 200:
            print(f"   ! API code {meta.get('code')}: {meta.get('error', {})}")
            break

        items = (data.get("result") or {}).get("items", [])
        if not items:
            break

        results.extend(items)
        total = (data.get("result") or {}).get("total", 0)
        print(f"   страница {page}: +{len(items)} (всего по запросу ~{total})")

        if page * PAGE_SIZE >= total:
            break
        page += 1
        time.sleep(REQUEST_DELAY)

    return results


def main():
    ap = argparse.ArgumentParser(description="Парсер лидов 2ГИС для TrustControl")
    ap.add_argument("--city", default="Алматы", help="Город (по умолчанию Алматы)")
    ap.add_argument("--categories", nargs="*", default=list(DEFAULT_CATEGORIES.keys()),
                    help="Категории: кофейня аптека фастфуд")
    ap.add_argument("--pages", type=int, default=20,
                    help="Макс. страниц на запрос (50 бизнесов/страница)")
    ap.add_argument("--only-with-phone", action="store_true", default=True,
                    help="Сохранять только лиды с телефоном (для WhatsApp)")
    args = ap.parse_args()

    if not API_KEY:
        print("❌ Нет ключа. Получи бесплатный на https://dev.2gis.ru и задай:")
        print("   export DGIS_API_KEY=ваш_ключ   (Windows: set DGIS_API_KEY=...)")
        sys.exit(1)

    seen = set()        # дедуп по (название+адрес)
    rows = []

    for cat in args.categories:
        queries = DEFAULT_CATEGORIES.get(cat, [cat])
        print(f"\n🔎 Категория «{cat}» — запросы: {', '.join(queries)}")
        for q in queries:
            print(f"  → «{q} {args.city}»")
            for item in fetch_category(q, args.city, args.pages):
                name = item.get("name", "").strip()
                address = item.get("address_name") or item.get("address", {}).get("name", "") if isinstance(item.get("address"), dict) else item.get("address_name", "")
                key = (name.lower(), str(address).lower())
                if not name or key in seen:
                    continue
                seen.add(key)

                phones = _extract_phones(item)
                if args.only_with_phone and not phones:
                    continue

                rating, reviews = _extract_rating(item)
                point = item.get("point", {}) or {}
                item_id = item.get("id", "")
                rows.append({
                    "category":   cat,
                    "name":       name,
                    "phone":      phones[0] if phones else "",
                    "phones_all": ";".join(phones),
                    "address":    address,
                    "rating":     rating,
                    "reviews":    reviews,
                    "website":    _extract_website(item),
                    "lat":        point.get("lat", ""),
                    "lon":        point.get("lon", ""),
                    "twogis_url": f"https://2gis.kz/almaty/firm/{item_id}" if item_id else "",
                    "contacted":  "",     # заполняется whatsapp_outreach.py
                })

    if not rows:
        print("\n⚠ Ничего не найдено. Проверь ключ и категории.")
        sys.exit(0)

    # Сортируем по рейтингу (сначала с отзывами — теплее и платёжеспособнее)
    rows.sort(key=lambda x: (float(x["rating"] or 0), int(x["reviews"] or 0)), reverse=True)

    with open(OUT_FILE, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    with_phone = sum(1 for r in rows if r["phone"])
    print(f"\n✅ Готово: {len(rows)} лидов ({with_phone} с телефоном)")
    print(f"   Файл: {OUT_FILE}")
    print(f"   Дальше: python whatsapp_outreach.py")


if __name__ == "__main__":
    main()
