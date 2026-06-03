#!/usr/bin/env python3
# ════════════════════════════════════════════════════════════
#  TrustControl — Генератор лидов из 2ГИС (режим «сбор урожая»)
#
#  2ГИС заблокировал прямые вызовы каталога даже с живым ключом.
#  Поэтому НЕ делаем свои запросы — открываем реальные страницы
#  поиска 2ГИС постранично, а скрипт перехватывает ответы, которые
#  САМ САЙТ уже загрузил (через сетевые логи Chrome / CDP).
#  Это настоящие запросы сайта со всеми подписями — блокировать нечего.
#
#  Запуск (на своём ПК, где есть Chrome):
#    python -m pip install selenium webdriver-manager openai
#    python scripts\leadgen_2gis.py --niche coffee --limit 200
#    python scripts\leadgen_2gis.py --niche coffee --analyze 30   # + GPT-питч
# ════════════════════════════════════════════════════════════

import argparse
import csv
import json
import math
import os
import sys
import time
import urllib.parse
from dataclasses import dataclass, asdict
from typing import Optional

NICHES = {
    "coffee":   ["кофейня", "кофе с собой"],
    "fastfood": ["фастфуд", "донер", "бургерная", "шаурма"],
    "cafe":     ["кафе", "столовая"],
    "beauty":   ["салон красоты", "барбершоп", "парикмахерская"],
    "all":      ["кофейня", "фастфуд", "кафе", "донер", "бургерная"],
}

# Рубрики-мусор: заправки, супермаркеты, аптеки, продуктовые — выкидываем.
JUNK_RUBRIC_WORDS = (
    "азс", "заправ", "супермаркет", "гипермаркет", "аптек", "продуктовый",
    "магазин продуктов", "автозаправ", "топлив", "нефт",
)

# slug города в URL 2gis.kz
CITY_SLUG = {
    "алматы": "almaty", "астана": "astana", "нур-султан": "astana",
    "шымкент": "shymkent", "актобе": "aktobe", "караганда": "karaganda",
    "тараз": "taraz", "павлодар": "pavlodar", "атырау": "atyrau",
    "костанай": "kostanay", "уральск": "uralsk", "актау": "aktau",
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
    heat:          float = 0.0
    pain:          str = ""
    pitch:         str = ""


# ── Selenium ──────────────────────────────────────────────────────────────────

def make_driver():
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.chrome.service import Service
    except ImportError:
        sys.exit("python -m pip install selenium webdriver-manager")

    opts = Options()
    opts.add_argument("--user-data-dir=" + os.path.abspath("dgis_profile"))
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument("--window-size=1280,900")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    opts.set_capability("goog:loggingPrefs", {"performance": "ALL"})

    try:
        from webdriver_manager.chrome import ChromeDriverManager
        driver = webdriver.Chrome(
            service=Service(ChromeDriverManager().install()), options=opts
        )
    except Exception:
        driver = webdriver.Chrome(options=opts)

    driver.execute_cdp_cmd("Network.enable", {})
    driver.execute_script(
        "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
    )
    return driver


def harvest_responses(driver, seen_rids: set) -> list[dict]:
    """
    Читает сетевые логи Chrome, забирает тела ответов от API 2ГИС,
    которые сайт уже загрузил. Возвращает распарсенные JSON-объекты.
    """
    out = []
    try:
        logs = driver.get_log("performance")
    except Exception:
        return out

    for entry in logs:
        try:
            msg = json.loads(entry["message"])["message"]
        except Exception:
            continue
        if msg.get("method") != "Network.responseReceived":
            continue
        p = msg.get("params", {})
        url = p.get("response", {}).get("url", "")
        rid = p.get("requestId")
        if not rid or rid in seen_rids:
            continue
        # нас интересуют любые ответы каталога/поиска 2ГИС
        if "2gis" not in url or "items" not in url and "catalog" not in url:
            continue
        seen_rids.add(rid)
        try:
            body = driver.execute_cdp_cmd(
                "Network.getResponseBody", {"requestId": rid}
            )
            data = json.loads(body.get("body", ""))
            out.append(data)
        except Exception:
            continue
    return out


def _items_from_payload(data: dict) -> list[dict]:
    """Достаёт список заведений из ответа 2ГИС (структура может отличаться)."""
    result = data.get("result")
    if isinstance(result, dict) and isinstance(result.get("items"), list):
        return result["items"]
    if isinstance(data.get("items"), list):
        return data["items"]
    return []


# ── Сбор лидов по нише ─────────────────────────────────────────────────────────

def collect(driver, query: str, city_slug: str, limit: int, sink: dict):
    """Идёт по страницам поиска, собирает заведения из перехваченных ответов."""
    seen_rids: set = set()
    base = f"https://2gis.kz/{city_slug}/search/{urllib.parse.quote(query)}"
    max_pages = min(max(1, math.ceil(limit / 12)), 15)

    for page in range(1, max_pages + 1):
        url = base if page == 1 else f"{base}/page/{page}"
        driver.get(url)
        # ждём загрузку + даём сайту дофетчить
        found_here = 0
        for _ in range(5):
            time.sleep(1.5)
            try:
                driver.execute_script("window.scrollBy(0, 800);")
            except Exception:
                pass
            for data in harvest_responses(driver, seen_rids):
                for it in _items_from_payload(data):
                    lead = _parse_item(it, query)
                    if _is_junk(lead):
                        continue
                    key = lead.dgis_id or lead.name
                    if key and key not in sink:
                        sink[key] = lead
                        found_here += 1
        print(f"    стр.{page}: +{found_here} (всего {len(sink)})")
        if found_here == 0 and page > 1:
            break  # страницы кончились
        if len(sink) >= limit:
            break


def _is_junk(lead: Lead) -> bool:
    """Отсеивает заправки, супермаркеты, аптеки и т.п."""
    text = f"{lead.rubric} {lead.name}".lower()
    return any(w in text for w in JUNK_RUBRIC_WORDS)


def enrich_from_card(driver, lead: Lead):
    """
    Заходит на карточку фирмы и дозабирает реальное число филиалов + телефон.
    2ГИС отдаёт branch_count и контакты только на детальной странице.
    """
    if not lead.dgis_id:
        return
    seen_rids: set = set()
    driver.get(f"https://2gis.kz/firm/{lead.dgis_id}")
    for _ in range(4):
        time.sleep(1.2)
        for data in harvest_responses(driver, seen_rids):
            for it in _items_from_payload(data):
                if str(it.get("id", "")).split("_")[0] != lead.dgis_id:
                    continue
                org = it.get("org") or {}
                bc = org.get("branch_count")
                if bc:
                    lead.branch_count = int(bc)
                if not lead.phone:
                    for grp in it.get("contact_groups", []) or []:
                        for c in grp.get("contacts", []) or []:
                            if c.get("type") == "phone":
                                lead.phone = c.get("value") or c.get("text") or ""
                                break
                        if lead.phone:
                            break
                return


def _parse_item(it: dict, query: str) -> Lead:
    phone = ""
    for grp in it.get("contact_groups", []) or []:
        for c in grp.get("contacts", []) or []:
            if c.get("type") == "phone":
                phone = c.get("value") or c.get("text") or ""
                break
        if phone:
            break

    reviews = it.get("reviews") or {}
    rating  = reviews.get("general_rating")
    rcount  = reviews.get("general_review_count") or reviews.get("review_count") or 0

    org = it.get("org") or {}
    branch_count = org.get("branch_count") or 1

    rubrics = it.get("rubrics") or []
    rubric  = rubrics[0].get("name") if rubrics and isinstance(rubrics[0], dict) else query

    addr = it.get("address_name", "")
    if not addr and isinstance(it.get("address"), dict):
        addr = it["address"].get("name", "")

    dgis_id = str(it.get("id", "")).split("_")[0]

    return Lead(
        name          = it.get("name", "?"),
        address       = addr,
        phone         = phone,
        rating        = float(rating) if rating is not None else None,
        reviews_count = int(rcount),
        branch_count  = int(branch_count),
        rubric        = rubric,
        dgis_id       = dgis_id,
        url_2gis      = f"https://2gis.kz/firm/{dgis_id}" if dgis_id else "",
    )


# ── Температура лида ──────────────────────────────────────────────────────────

def score_heat(lead: Lead) -> float:
    heat = 0.0
    if lead.rating is not None and lead.reviews_count >= 5:
        if   lead.rating < 3.5: heat += 50
        elif lead.rating < 4.0: heat += 38
        elif lead.rating < 4.3: heat += 25
        elif lead.rating < 4.6: heat += 12
        else:                   heat += 4
    else:
        heat += 15

    if   lead.branch_count >= 10: heat += 35
    elif lead.branch_count >= 5:  heat += 30
    elif lead.branch_count >= 2:  heat += 22
    else:                         heat += 8

    if   lead.reviews_count >= 200: heat += 15
    elif lead.reviews_count >= 50:  heat += 11
    elif lead.reviews_count >= 10:  heat += 6
    else:                           heat += 2

    return round(min(heat, 100.0), 1)


# ── GPT-анализ ────────────────────────────────────────────────────────────────

def analyze_with_gpt(lead: Lead, openai_key: str) -> tuple[str, str]:
    try:
        from openai import OpenAI
    except ImportError:
        return "", ""
    client = OpenAI(api_key=openai_key)
    prompt = (
        "Ты аналитик продаж TrustControl (ИИ-контроль качества обслуживания на кассе: "
        "ловит хамство, отсутствие приветствия, обсчёт, вялых кассиров).\n\n"
        f"Заведение: {lead.name} ({lead.rubric}), {lead.address}.\n"
        f"Рейтинг: {lead.rating}, отзывов: {lead.reviews_count}, филиалов: {lead.branch_count}.\n\n"
        "1) Одно предложение — главная БОЛЬ с обслуживанием (что решает TrustControl).\n"
        "2) Одно предложение — ПОДСКАЗКА под звонок ЛПР: на что давить.\n"
        "JSON: {\"pain\": \"...\", \"pitch\": \"...\"}"
    )
    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=250, temperature=0.4,
            response_format={"type": "json_object"},
        )
        data = json.loads(resp.choices[0].message.content)
        return data.get("pain", ""), data.get("pitch", "")
    except Exception as e:
        print(f"  [GPT] {lead.name}: {e}", file=sys.stderr)
        return "", ""


# ── Сохранение ────────────────────────────────────────────────────────────────

def save(leads: list[Lead], stem: str):
    with open(f"{stem}.json", "w", encoding="utf-8") as f:
        json.dump([asdict(l) for l in leads], f, ensure_ascii=False, indent=2)
    with open(f"{stem}.csv", "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Температура", "Название", "Рейтинг", "Отзывов", "Филиалов",
                    "Телефон", "Адрес", "Боль", "Подсказка", "2ГИС"])
        for l in leads:
            w.writerow([l.heat, l.name, l.rating, l.reviews_count, l.branch_count,
                        l.phone, l.address, l.pain, l.pitch, l.url_2gis])
    print(f"\nГотово: {stem}.csv и {stem}.json — {len(leads)} лидов")


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--city",    default="Алматы")
    ap.add_argument("--niche",   default="coffee", choices=list(NICHES))
    ap.add_argument("--limit",   type=int, default=200)
    ap.add_argument("--enrich",  type=int, default=60,
                    help="зайти на карточки топ-N за реальными филиалами+телефоном")
    ap.add_argument("--analyze", type=int, default=0, help="топ-N через GPT")
    ap.add_argument("--out",     default=None)
    args = ap.parse_args()

    city_slug = CITY_SLUG.get(args.city.strip().lower(), "almaty")
    print(f"Город: {args.city} ({city_slug}) | Ниша: {args.niche}")
    print("Запускаю Chrome (окно откроется — не закрывай)...")

    driver = make_driver()
    sink: dict = {}
    try:
        for q in NICHES[args.niche]:
            print(f"  Ниша-запрос: «{q}»")
            collect(driver, q, city_slug, args.limit, sink)

        leads = list(sink.values())
        for l in leads:
            l.heat = score_heat(l)
        leads.sort(key=lambda x: x.heat, reverse=True)
        print(f"\nСобрано уникальных (без мусора): {len(leads)}")

        # дозагрузка карточек топ-лидов: реальные филиалы + телефон
        if args.enrich > 0 and leads:
            top = leads[:args.enrich]
            print(f"Дозагружаю карточки топ-{len(top)} (филиалы + телефоны)...")
            for i, l in enumerate(top, 1):
                try:
                    enrich_from_card(driver, l)
                except Exception as e:
                    print(f"  [!] {l.name}: {e}", file=sys.stderr)
                l.heat = score_heat(l)  # пересчёт с реальными филиалами
                if i % 10 == 0:
                    print(f"  ...{i}/{len(top)}")
            leads.sort(key=lambda x: x.heat, reverse=True)

        if args.analyze > 0 and leads:
            openai_key = os.getenv("OPENAI_API_KEY")
            if not openai_key:
                print("OPENAI_API_KEY не задан — пропускаю GPT", file=sys.stderr)
            else:
                print(f"GPT-анализ топ-{args.analyze}...")
                for l in leads[:args.analyze]:
                    l.pain, l.pitch = analyze_with_gpt(l, openai_key)
                    print(f"  ✓ {l.name}")
                    time.sleep(0.3)

        stem = args.out or f"leads_{args.city}_{args.niche}".replace(" ", "_")
        save(leads, stem)

        print("\nТоп-10 горячих лидов:")
        for l in leads[:10]:
            print(f"  {l.heat:5}  {l.name[:36]:36}  ★{l.rating}  "
                  f"отз.{l.reviews_count}  фил.{l.branch_count}  {l.phone}")
    finally:
        print("\nЗакрываю браузер...")
        try:
            driver.quit()
        except Exception:
            pass


if __name__ == "__main__":
    main()
