#!/usr/bin/env python3
# ════════════════════════════════════════════════════════════
#  TrustControl — Генератор лидов из 2ГИС
#
#  Что делает:
#    1. Тянет из каталога 2ГИС заведения по нише и городу
#       (кофейни, фастфуд, кафе — настраивается).
#    2. По каждому собирает: название, адрес, телефон, рейтинг,
#       число отзывов, число филиалов.
#    3. Считает «температуру лида» — насколько это горячий клиент
#       именно для TrustControl (низкий рейтинг = боль с сервисом,
#       сеть филиалов = есть ЛПР, который не уследит за кассами).
#    4. (опц.) По горячим лидам тянет тексты отзывов и через GPT
#       находит КОНКРЕТНУЮ боль + пишет подсказку под звонок.
#    5. Выгружает результат в CSV и JSON, отсортированный по температуре.
#
#  ВАЖНО: запускать НЕ в облаке Claude (там нет интернета),
#         а на своём ПК или на VPS, где есть выход в сеть.
#
#  Запуск:
#    export DGIS_API_KEY=...           # бесплатный ключ dev.2gis.com
#    export OPENAI_API_KEY=sk-...      # опц., для анализа отзывов
#    python scripts/leadgen_2gis.py --city Алматы --niche coffee --limit 200
#
#  Ключ 2ГИС (бесплатный Catalog API):
#    https://dev.2gis.com/  → зарегистрироваться → создать ключ
#    Бесплатного тарифа хватает на тысячи запросов.
# ════════════════════════════════════════════════════════════

import argparse
import csv
import json
import os
import sys
import time
from dataclasses import dataclass, asdict, field
from typing import Optional

try:
    import requests
except ImportError:
    sys.exit("Нужен модуль requests:  pip install requests")

CATALOG = "https://catalog.api.2gis.com/3.0/items"

# Публичный ключ, которым пользуется сам сайт 2gis.kz (достаётся из DevTools).
# Может смениться или ограничиться по IP — тогда возьми новый из Network
# и передай через --key или переменную DGIS_API_KEY.
DEFAULT_WEB_KEY = "c7f1a769-c8a5-4636-b14d-d8c987808a12"

# Координаты центра городов (lon, lat) — как в адресной строке 2gis.kz (m=lon,lat).
# Веб-ключ не пускает в справочник регионов, поэтому ищем по точке + городу в запросе.
CITY_COORDS = {
    "алматы":     (76.889709, 43.238949),
    "астана":     (71.430411, 51.128422),
    "нур-султан": (71.430411, 51.128422),
    "шымкент":    (69.596500, 42.317000),
    "караганда":  (73.087500, 49.806400),
    "актобе":     (57.166000, 50.283900),
    "тараз":      (71.378900, 42.901600),
    "павлодар":   (76.967100, 52.287300),
    "усть-каменогорск": (82.617800, 49.948600),
    "семей":      (80.227500, 50.411100),
    "атырау":     (51.923900, 47.094500),
    "костанай":   (63.624200, 53.214400),
    "кызылорда":  (65.509500, 44.842800),
    "уральск":    (51.366800, 51.227700),
    "петропавловск": (69.146100, 54.872800),
    "актау":      (51.158900, 43.651100),
}

# Ниши → поисковые запросы 2ГИС (рубрики).
NICHES = {
    "coffee":   ["кофейня", "кофе с собой"],
    "fastfood": ["фастфуд", "донер", "бургерная", "шаурма"],
    "cafe":     ["кафе", "столовая"],
    "beauty":   ["салон красоты", "барбершоп", "парикмахерская"],
    "all":      ["кофейня", "фастфуд", "кафе", "донер", "бургерная"],
}


@dataclass
class Lead:
    name:          str
    address:       str = ""
    phone:         str = ""
    rating:        Optional[float] = None
    reviews_count: int = 0
    branch_count:  int = 1
    rubric:        str = ""
    dgis_id:       str = ""
    url_2gis:      str = ""
    # заполняется анализом
    heat:          float = 0.0
    pain:          str = ""
    pitch:         str = ""


# ── 2ГИС API ──────────────────────────────────────────────────────────────────

def fetch_leads(query: str, city: str, key: str, limit: int) -> list[Lead]:
    """
    Тянет заведения по одному запросу с пагинацией.
    Ищем по координатам центра города (как сам сайт) + город в тексте запроса,
    т.к. веб-ключ не пускает в справочник регионов.
    """
    out: list[Lead] = []
    page = 1
    page_size = 50  # максимум 2ГИС
    fields = (
        "items.point,items.address,items.contact_groups,"
        "items.reviews,items.rubrics,items.org"
    )
    coords = CITY_COORDS.get(city.strip().lower())
    full_query = f"{query} {city}"

    while len(out) < limit:
        params = {
            "q":         full_query,
            "page":      page,
            "page_size": page_size,
            "fields":    fields,
            "key":       key,
        }
        if coords:
            params["location"] = f"{coords[0]},{coords[1]}"
        r = requests.get(CATALOG, params=params, timeout=30)
        if r.status_code == 404:
            break  # 2ГИС отдаёт 404 когда страницы кончились
        r.raise_for_status()
        payload = r.json()
        meta = payload.get("meta") or {}
        if meta.get("code") not in (200, None):
            print(f"    [2ГИС] {meta.get('code')}: {meta.get('error', {})}", file=sys.stderr)
            break
        result = payload.get("result") or {}
        items = result.get("items") or []
        if not items:
            break

        for it in items:
            out.append(_parse_item(it, query))

        total = result.get("total", 0)
        if page * page_size >= total:
            break
        page += 1
        time.sleep(0.25)  # вежливость к API

    return out[:limit]


def _parse_item(it: dict, query: str) -> Lead:
    # телефон
    phone = ""
    for grp in it.get("contact_groups", []):
        for c in grp.get("contacts", []):
            if c.get("type") == "phone":
                phone = c.get("value") or c.get("text") or ""
                break
        if phone:
            break

    # рейтинг / отзывы
    reviews = it.get("reviews") or {}
    rating  = reviews.get("general_rating")
    rcount  = reviews.get("general_review_count") or 0

    # филиалы
    org = it.get("org") or {}
    branch_count = org.get("branch_count") or 1

    # рубрика
    rubrics = it.get("rubrics") or []
    rubric = rubrics[0]["name"] if rubrics else query

    dgis_id = str(it.get("id", "")).split("_")[0]

    return Lead(
        name          = it.get("name", "?"),
        address       = it.get("address_name", "") or it.get("address", {}).get("name", "") if isinstance(it.get("address"), dict) else it.get("address_name", ""),
        phone         = phone,
        rating        = float(rating) if rating is not None else None,
        reviews_count = int(rcount),
        branch_count  = int(branch_count),
        rubric        = rubric,
        dgis_id       = dgis_id,
        url_2gis      = f"https://2gis.kz/firm/{dgis_id}" if dgis_id else "",
    )


# ── Температура лида ────────────────────────────────────────────────────────────

def score_heat(lead: Lead) -> float:
    """
    0..100. Чем выше — тем горячее клиент для TrustControl.

    Логика:
      • Низкий рейтинг при заметном числе отзывов = реальная боль с сервисом
        (хамство/обсчёт/долгое обслуживание — наш профиль).
      • Сеть филиалов = есть владелец/управляющий (ЛПР), который физически
        не уследит за каждой кассой = максимально наш клиент.
      • Совсем без отзывов = непонятно, ставим средне.
    """
    heat = 0.0

    # 1. Боль из рейтинга (макс 50)
    if lead.rating is not None and lead.reviews_count >= 5:
        if   lead.rating < 3.5: heat += 50
        elif lead.rating < 4.0: heat += 38
        elif lead.rating < 4.3: heat += 25
        elif lead.rating < 4.6: heat += 12
        else:                   heat += 4
    else:
        heat += 15  # нет данных — нейтрально

    # 2. Платёжеспособность + наличие ЛПР через число филиалов (макс 35)
    if   lead.branch_count >= 10: heat += 35
    elif lead.branch_count >= 5:  heat += 30
    elif lead.branch_count >= 2:  heat += 22
    else:                         heat += 8

    # 3. Активность аудитории (макс 15) — много отзывов = живой трафик
    if   lead.reviews_count >= 200: heat += 15
    elif lead.reviews_count >= 50:  heat += 11
    elif lead.reviews_count >= 10:  heat += 6
    else:                           heat += 2

    return round(min(heat, 100.0), 1)


# ── Анализ отзывов через GPT (опционально) ─────────────────────────────────────

def fetch_reviews_text(dgis_id: str, key: str, limit: int = 8) -> list[str]:
    """
    Пытается вытащить тексты отзывов через Catalog API.
    Если 2ГИС не отдаёт тексты по этому ключу — вернёт пустой список,
    и анализ деградирует до оценки по рейтингу.
    """
    try:
        r = requests.get(
            CATALOG,
            params={
                "id":     dgis_id,
                "fields": "items.reviews",
                "key":    key,
            },
            timeout=30,
        )
        r.raise_for_status()
        items = (r.json().get("result") or {}).get("items") or []
        if not items:
            return []
        reviews = (items[0].get("reviews") or {}).get("items") or []
        texts = [rv.get("text", "").strip() for rv in reviews if rv.get("text")]
        return texts[:limit]
    except Exception:
        return []


def analyze_with_gpt(lead: Lead, reviews: list[str], openai_key: str) -> tuple[str, str]:
    """Возвращает (боль, подсказка под звонок). При ошибке — пустые строки."""
    try:
        from openai import OpenAI
    except ImportError:
        return "", ""

    client = OpenAI(api_key=openai_key)
    reviews_block = "\n".join(f"- {t}" for t in reviews[:8]) or "(текстов отзывов нет)"
    prompt = (
        "Ты — аналитик продаж SaaS TrustControl (ИИ-контроль качества обслуживания "
        "на кассе: ловит хамство, отсутствие приветствия, обсчёт клиентов, вялых кассиров).\n\n"
        f"Заведение: {lead.name} ({lead.rubric}), Алматы.\n"
        f"Рейтинг 2ГИС: {lead.rating}, отзывов: {lead.reviews_count}, филиалов: {lead.branch_count}.\n"
        f"Отзывы:\n{reviews_block}\n\n"
        "Задача:\n"
        "1) В одном предложении — главная БОЛЬ с обслуживанием, которую решает TrustControl.\n"
        "2) В одном предложении — ПОДСКАЗКА под звонок ЛПР: на что давить, чтобы зацепить.\n"
        "Ответь строго JSON: {\"pain\": \"...\", \"pitch\": \"...\"}"
    )
    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=300,
            temperature=0.4,
            response_format={"type": "json_object"},
        )
        data = json.loads(resp.choices[0].message.content)
        return data.get("pain", ""), data.get("pitch", "")
    except Exception as e:
        print(f"  [GPT] ошибка для {lead.name}: {e}", file=sys.stderr)
        return "", ""


# ── Выгрузка ────────────────────────────────────────────────────────────────────

def save(leads: list[Lead], stem: str):
    with open(f"{stem}.json", "w", encoding="utf-8") as f:
        json.dump([asdict(l) for l in leads], f, ensure_ascii=False, indent=2)

    with open(f"{stem}.csv", "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Температура", "Название", "Рейтинг", "Отзывов", "Филиалов",
                    "Телефон", "Адрес", "Боль", "Подсказка", "2ГИС"])
        for l in leads:
            w.writerow([l.heat, l.name, l.rating, l.reviews_count, l.branch_count,
                        l.phone, l.address, l.pain, l.pitch, l.url_2gis])

    print(f"\nГотово: {stem}.csv и {stem}.json — {len(leads)} лидов")


# ── main ─────────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Генератор лидов из 2ГИС для TrustControl")
    ap.add_argument("--city",  default="Алматы")
    ap.add_argument("--niche", default="coffee", choices=list(NICHES))
    ap.add_argument("--limit", type=int, default=200, help="макс заведений на запрос")
    ap.add_argument("--analyze", type=int, default=0,
                    help="сколько верхних лидов прогнать через GPT (0 = не анализировать)")
    ap.add_argument("--out", default=None, help="префикс выходных файлов")
    ap.add_argument("--key", default=None, help="ключ 2ГИС (если встроенный перестал работать)")
    args = ap.parse_args()

    key = args.key or os.getenv("DGIS_API_KEY") or DEFAULT_WEB_KEY

    print(f"Город: {args.city} | Ниша: {args.niche}")
    if args.city.strip().lower() not in CITY_COORDS:
        print(f"  (центр города «{args.city}» неизвестен — ищу только по тексту, "
              f"результаты могут быть менее точными)")

    # Собираем по всем запросам ниши, дедупим по dgis_id
    seen: dict[str, Lead] = {}
    for q in NICHES[args.niche]:
        print(f"  Запрос: {q} ...")
        for lead in fetch_leads(q, args.city, key, args.limit):
            if lead.dgis_id and lead.dgis_id not in seen:
                seen[lead.dgis_id] = lead

    leads = list(seen.values())
    for l in leads:
        l.heat = score_heat(l)
    leads.sort(key=lambda x: x.heat, reverse=True)
    print(f"Собрано уникальных: {len(leads)}")

    # GPT-анализ верхних
    if args.analyze > 0:
        openai_key = os.getenv("OPENAI_API_KEY")
        if not openai_key:
            print("OPENAI_API_KEY не задан — пропускаю GPT-анализ", file=sys.stderr)
        else:
            print(f"Анализирую топ-{args.analyze} через GPT ...")
            for l in leads[:args.analyze]:
                reviews = fetch_reviews_text(l.dgis_id, key)
                l.pain, l.pitch = analyze_with_gpt(l, reviews, openai_key)
                print(f"  ✓ {l.name} (heat={l.heat})")
                time.sleep(0.3)

    stem = args.out or f"leads_{args.city}_{args.niche}".replace(" ", "_")
    save(leads, stem)

    # Превью топ-10
    print("\nТоп-10 горячих лидов:")
    for l in leads[:10]:
        print(f"  {l.heat:5}  {l.name[:35]:35}  ★{l.rating}  отз.{l.reviews_count}  фил.{l.branch_count}")


if __name__ == "__main__":
    main()
