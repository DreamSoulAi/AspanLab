#!/usr/bin/env python3
# ════════════════════════════════════════════════════════════
#  TrustControl — Генератор лидов из 2ГИС (Selenium-режим)
#
#  Запуск (на своём ПК, где есть Chrome):
#    pip install selenium webdriver-manager openai
#    python scripts\leadgen_2gis.py --niche coffee --limit 200
#    python scripts\leadgen_2gis.py --niche coffee --analyze 30   # + GPT-питч
# ════════════════════════════════════════════════════════════

import argparse
import csv
import json
import os
import sys
import time
import urllib.parse
from dataclasses import dataclass, asdict
from typing import Optional

CATALOG = "https://catalog.api.2gis.com/3.0/items"
FIELDS  = (
    "items.point,items.address,items.contact_groups,"
    "items.reviews,items.rubrics,items.org"
)

NICHES = {
    "coffee":   ["кофейня", "кофе с собой"],
    "fastfood": ["фастфуд", "донер", "бургерная", "шаурма"],
    "cafe":     ["кафе", "столовая"],
    "beauty":   ["салон красоты", "барбершоп", "парикмахерская"],
    "all":      ["кофейня", "фастфуд", "кафе", "донер", "бургерная"],
}

CITY_COORDS = {
    "алматы":     (76.889709, 43.238949),
    "астана":     (71.430411, 51.128422),
    "нур-султан": (71.430411, 51.128422),
    "шымкент":    (69.596500, 42.317000),
    "актобе":     (57.166000, 50.283900),
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
        sys.exit("pip install selenium webdriver-manager")

    opts = Options()
    opts.add_argument("--user-data-dir=" + os.path.abspath("dgis_profile"))
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    # включаем логирование сети Chrome (CDP) — оттуда достанем рабочий ключ
    opts.set_capability("goog:loggingPrefs", {"performance": "ALL"})
    # убираем headless — нужен видимый Chrome (иначе 2ГИС может не грузиться)

    try:
        from webdriver_manager.chrome import ChromeDriverManager
        driver = webdriver.Chrome(
            service=Service(ChromeDriverManager().install()),
            options=opts,
        )
    except Exception:
        # fallback если webdriver-manager не установлен
        driver = webdriver.Chrome(options=opts)

    driver.execute_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    )
    return driver


def _scan_key_from_logs(driver) -> Optional[str]:
    """Читает CDP-логи сети и ищет key= в запросах к catalog.api.2gis."""
    import re
    try:
        logs = driver.get_log("performance")
    except Exception:
        return None
    for entry in logs:
        try:
            msg = json.loads(entry["message"])["message"]
        except Exception:
            continue
        if msg.get("method") not in (
            "Network.requestWillBeSent", "Network.responseReceived"
        ):
            continue
        params = msg.get("params", {})
        url = (params.get("request", {}).get("url")
               or params.get("response", {}).get("url") or "")
        if "catalog.api.2gis" in url and "key=" in url:
            m = re.search(r"[?&]key=([a-zA-Z0-9\-]{20,})", url)
            if m:
                return m.group(1)
    return None


def get_live_key(driver) -> str:
    """
    Открывает поиск 2gis.kz и ловит рабочий ключ из сетевых логов Chrome.
    Скроллит список, чтобы спровоцировать запросы к каталогу.
    """
    print("  Открываю поиск 2gis.kz, ловлю живой ключ...")
    driver.get("https://2gis.kz/almaty/search/" + urllib.parse.quote("кофейня"))

    # ждём и периодически скроллим — пока не поймаем ключ (до ~25 сек)
    for i in range(12):
        time.sleep(2)
        try:
            driver.execute_script("window.scrollBy(0, 600);")
        except Exception:
            pass
        key = _scan_key_from_logs(driver)
        if key:
            print(f"  Живой ключ пойман: {key[:16]}...")
            return key

    sys.exit("Не удалось поймать ключ из сети 2ГИС. "
             "Проверь что окно Chrome открылось и страница загрузилась, запусти ещё раз.")


def api_call(driver, url: str) -> Optional[dict]:
    """
    Делает fetch() ИЗНУТРИ браузера на домене 2gis.kz.
    Ключ работает всегда — он в своём же браузерном контексте.
    """
    result = driver.execute_async_script("""
        const [url, callback] = arguments;
        fetch(url, {credentials: 'include'})
            .then(r => r.json())
            .then(d => callback({ok: true, data: d}))
            .catch(e => callback({ok: false, error: e.toString()}));
    """, url)

    if not result or not result.get("ok"):
        return None
    return result.get("data")


# ── Получение лидов ───────────────────────────────────────────────────────────

def fetch_leads(query: str, city: str, key: str, limit: int, driver) -> list[Lead]:
    out: list[Lead] = []
    page = 1
    page_size = 50
    coords = CITY_COORDS.get(city.strip().lower())
    full_q = f"{query} {city}"

    while len(out) < limit:
        params: dict = {
            "q":         full_q,
            "page":      page,
            "page_size": page_size,
            "fields":    FIELDS,
            "key":       key,
        }
        if coords:
            params["location"] = f"{coords[0]},{coords[1]}"

        url = CATALOG + "?" + urllib.parse.urlencode(params)
        payload = api_call(driver, url)

        if payload is None:
            print(f"    [!] Пустой ответ на странице {page}", file=sys.stderr)
            break

        meta = payload.get("meta") or {}
        code = meta.get("code")
        if code and code != 200:
            print(f"    [2ГИС] {code}: {meta.get('error', '')}", file=sys.stderr)
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
        time.sleep(0.3)

    return out[:limit]


def _parse_item(it: dict, query: str) -> Lead:
    phone = ""
    for grp in it.get("contact_groups", []):
        for c in grp.get("contacts", []):
            if c.get("type") == "phone":
                phone = c.get("value") or c.get("text") or ""
                break
        if phone:
            break

    reviews = it.get("reviews") or {}
    rating  = reviews.get("general_rating")
    rcount  = reviews.get("general_review_count") or 0

    org = it.get("org") or {}
    branch_count = org.get("branch_count") or 1

    rubrics = it.get("rubrics") or []
    rubric  = rubrics[0]["name"] if rubrics else query

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
        "1) Одно предложение — главная БОЛЬ с обслуживанием (что именно решает TrustControl).\n"
        "2) Одно предложение — ПОДСКАЗКА под звонок ЛПР: на что давить.\n"
        "JSON: {\"pain\": \"...\", \"pitch\": \"...\"}"
    )
    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=250,
            temperature=0.4,
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
    ap.add_argument("--analyze", type=int, default=0,
                    help="топ-N лидов прогнать через GPT")
    ap.add_argument("--out",     default=None)
    args = ap.parse_args()

    print(f"Город: {args.city} | Ниша: {args.niche}")
    print("Запускаю Chrome...")

    driver = make_driver()
    try:
        key = get_live_key(driver)

        seen: dict[str, Lead] = {}
        for q in NICHES[args.niche]:
            print(f"  Запрос: «{q}» ...")
            for lead in fetch_leads(q, args.city, key, args.limit, driver):
                if lead.dgis_id and lead.dgis_id not in seen:
                    seen[lead.dgis_id] = lead
                elif not lead.dgis_id:
                    seen[lead.name] = lead

        leads = list(seen.values())
        for l in leads:
            l.heat = score_heat(l)
        leads.sort(key=lambda x: x.heat, reverse=True)
        print(f"Собрано уникальных: {len(leads)}")

        if args.analyze > 0:
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
            print(f"  {l.heat:5}  {l.name[:38]:38}  ★{l.rating}  "
                  f"отз.{l.reviews_count}  фил.{l.branch_count}  {l.phone}")

    finally:
        print("\nЗакрываю браузер...")
        driver.quit()


if __name__ == "__main__":
    main()
