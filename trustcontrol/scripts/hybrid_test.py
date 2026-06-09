"""
hybrid_test.py — проверка ГИБРИДНОГО движка. Самодостаточный, как compare_stt.py.

Кидаешь этот файл рядом с .wav и запускаешь — НИЧЕГО из проекта не нужно.
Делает то же что будет в проде:

    ISSAI ‖ OpenAI  →  GPT объединяет лучшее + чинит врачок→рожок, кера→QR  →  анализ

──────────────────────────────────────────────────────────────────────
ЗАПУСК (Windows, ровно как вчера):

  1) положи этот файл рядом с .wav
  2) set OPENAI_API_KEY=sk-...
  3) set ISSAI_API_KEY=<ключ воркера, если есть; иначе пропусти>
  4) pip install openai httpx
  5) python hybrid_test.py

Результат в консоль + hybrid_result.md
──────────────────────────────────────────────────────────────────────
"""

import os
import glob
import time

import httpx
from openai import OpenAI

# ── Настройки ──
ISSAI_URL = os.getenv("ISSAI_WORKER_URL", "http://213.155.21.25:8010")
ISSAI_KEY = os.getenv("ISSAI_API_KEY", "")          # пусто = без авторизации
OPENAI_STT = os.getenv("STT_MODEL", "gpt-4o-transcribe")   # первичка как в проде

PROMPT = (
    "Запись разговора с кассы в Казахстане. "
    "Транскрибируй ВСЕ голоса: кассира (близко к микрофону) И клиента "
    "(стоит дальше, голос тише — это не фон, это живой человек). "
    "Шала-казахский: русские слова с казахским акцентом и вставками "
    "сәлем, рахмет, ия, жоқ, теңге, картамен, не аласыз. "
    "Сохраняй оригинальный язык каждой фразы, не переводи."
)

MERGE_PROMPT = """Перед тобой ДВЕ транскрипции ОДНОЙ И ТОЙ ЖЕ аудиозаписи с кассы в Казахстане.
Это НЕ две разные записи и НЕ две половины разговора — это ОДИН разговор от начала до конца,
распознанный двумя разными движками. Оба варианта покрывают ВСЮ запись целиком: одну и ту же
таймлинию, одни и те же реплики, просто услышанные по-разному.

ЗАДАЧА: наложить эти два варианта друг на друга и собрать ОДИН транскрипт разговора,
где каждая реальная реплика встречается РОВНО ОДИН раз.

⛔ ГЛАВНОЕ ПРАВИЛО — НЕ СКЛЕИВАЙ ВСТЫК.
Запрещено выводить сначала весь ВАРИАНТ А, а потом весь ВАРИАНТ Б. Это ОДИН разговор, а не два.
Если просто допишешь Б после А — разговор задвоится (одни и те же заказ, суммы, клиент повторятся
дважды). Итоговый текст должен быть примерно ДЛИНЫ ОДНОГО варианта (самого длинного из двух),
а НЕ суммы обоих.

КАК НАКЛАДЫВАТЬ:
- Иди по разговору ПО ПОРЯДКУ один раз: приветствие → заказ → уточнения → оплата → прощание.
- Одна и та же реплика есть в обоих вариантах (просто распознана по-разному) → возьми более
  понятную версию и впиши её ОДИН раз, не дублируй.
- Казахские слова/фразы → предпочитай ВАРИАНТ А (ISSAI точнее на казахском).
- Русские слова/фразы → предпочитай ВАРИАНТ Б (OpenAI точнее на русском).
- Реплика есть только в одном варианте → включи, если она правдоподобна.
- Исправляй фонетические ошибки ISSAI по контексту: «врачок»/«брачок» → рожок;
  «кера»/«кюра»/«кьюар»/«кьйюар»/«кийюр» → QR; «сто кан» → стакан.
- Если один вариант явно галлюцинирует (несвязный мусор) — бери другой.
- НЕ придумывай слова, которых не было ни в одном варианте.
- Сохраняй языки: казахское слово — на казахском, русское — на русском.

ВАРИАНТ А — казахский распознаватель ISSAI (точен на казахском, русские слова коверкает фонетически,
может зашумить целые русские фразы).
ВАРИАНТ Б — OpenAI (точен на русском, на чистом казахском может выдумывать несуществующие фразы,
иногда пропускает тихий голос клиента).

Верни ТОЛЬКО итоговый текст одного разговора. Без объяснений, без кавычек, без JSON."""

ANALYZE_PROMPT = """Ты аудитор качества обслуживания на кассе в Казахстане. Перед тобой транскрипт.
Верни ТОЛЬКО JSON:
{"status":"OK|PERSONAL|IGNORE","score":<0-100>,"tone":"positive|neutral|negative",
"customers_served":<число>,"greeting":<bool>,"farewell":<bool>,"upsell":<bool>,
"rudeness":<bool>,"fraud":<bool>,"summary":"1-2 предложения на русском"}
OK = есть обслуживание клиента. PERSONAL = только болтовня сотрудников. IGNORE = мусор/тишина.
При сомнении выбирай OK. Шала-казахский и рваный текст — норма, не повод для IGNORE."""

client = OpenAI()   # ключ из OPENAI_API_KEY


def via_issai(path):
    """ISSAI VPS-воркер → (текст, мета-строка)."""
    headers = {"X-API-Key": ISSAI_KEY} if ISSAI_KEY else {}
    t0 = time.time()
    try:
        with open(path, "rb") as f:
            r = httpx.post(
                f"{ISSAI_URL.rstrip('/')}/transcribe",
                files={"audio": ("audio.wav", f, "audio/wav")},
                data={"language": "auto"},
                headers=headers, timeout=300.0,
            )
        dt = time.time() - t0
        if r.status_code != 200:
            return "", f"HTTP {r.status_code}: {r.text[:120]}"
        d = r.json()
        text = (d.get("text") or "").strip()
        meta = f"segments={d.get('segments')}, {dt:.1f}с"
        return text, meta
    except Exception as e:
        return "", f"ОШИБКА связи: {type(e).__name__}: {str(e)[:120]}"


def via_openai(path):
    """OpenAI STT → текст."""
    t0 = time.time()
    try:
        with open(path, "rb") as f:
            text = str(client.audio.transcriptions.create(
                model=OPENAI_STT, file=f, response_format="text", prompt=PROMPT
            )).strip()
        return text, f"{time.time()-t0:.1f}с"
    except Exception as e:
        return "", f"ОШИБКА: {type(e).__name__}: {str(e)[:120]}"


def merge(issai_text, openai_text):
    """GPT объединяет два транскрипта + чинит слова. Один пустой → берём другой."""
    a, b = (issai_text or "").strip(), (openai_text or "").strip()
    if not a:
        return b
    if not b:
        return a
    if a.lower() == b.lower():
        return b
    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content":
                       f"{MERGE_PROMPT}\n\nВАРИАНТ А (ISSAI):\n{a}\n\nВАРИАНТ Б (OpenAI):\n{b}"}],
            temperature=0.1, max_tokens=2000,
        )
        out = (resp.choices[0].message.content or "").strip().strip("«»\"'")
        if not out:
            return b
        # Страховка от склейки встык: A и B — одно аудио. Если merged ≈ сумме
        # длин обоих, GPT задвоил разговор → берём один (более длинный) транскрипт.
        longest = max(len(a), len(b))
        if len(out) > longest * 1.4:
            return a if len(a) >= len(b) else b
        return out
    except Exception as e:
        return f"(merge ошибка: {e}) {b}"


def analyze(text):
    """Краткий анализ merged-текста → строка для отчёта."""
    if not text or len(text.split()) < 2:
        return "пропущен (пусто → IGNORE)"
    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "system", "content": ANALYZE_PROMPT},
                      {"role": "user", "content": text}],
            response_format={"type": "json_object"},
            temperature=0.2, max_tokens=400,
        )
        import json
        d = json.loads(resp.choices[0].message.content)
        ev = [k for k in ("greeting", "farewell", "upsell", "rudeness", "fraud") if d.get(k)]
        return (f"status={d.get('status')}  score={d.get('score')}  tone={d.get('tone')}  "
                f"клиентов={d.get('customers_served')}\n"
                f"    события: {', '.join(ev) or '—'}\n"
                f"    {d.get('summary', '')}")
    except Exception as e:
        return f"ОШИБКА анализа: {e}"


def main():
    files = sorted(glob.glob("*.wav") + glob.glob("*.mp3") + glob.glob("*.m4a"))
    if not files:
        print("Не нашёл аудио (.wav/.mp3/.m4a) в этой папке. Положи записи рядом со скриптом.")
        return

    print(f"Файлов: {len(files)}  |  ISSAI: {ISSAI_URL} ({'ключ есть' if ISSAI_KEY else 'без ключа'})  |  OpenAI: {OPENAI_STT}\n")
    out = ["# Гибридный движок — проверка на записях кассы\n"]

    for path in files:
        print(f"\n{'='*64}\n=== {path} ===")
        out.append(f"\n## {path}\n")

        print("  [ISSAI] думаю (CPU, может быть долго)...")
        issai_text, issai_meta = via_issai(path)
        print(f"  [ISSAI {issai_meta}]\n    {issai_text or '(пусто)'}")
        out.append(f"**ISSAI** [{issai_meta}]\n```\n{issai_text or '(пусто)'}\n```\n")

        openai_text, openai_meta = via_openai(path)
        print(f"  [OpenAI {openai_meta}]\n    {openai_text or '(пусто)'}")
        out.append(f"**OpenAI {OPENAI_STT}** [{openai_meta}]\n```\n{openai_text or '(пусто)'}\n```\n")

        print("  [MERGE] объединяю...")
        merged = merge(issai_text, openai_text)
        print(f"  [🔀 MERGED — что увидит анализ]\n    {merged or '(пусто)'}")
        out.append(f"**🔀 MERGED**\n```\n{merged or '(пусто)'}\n```\n")

        res = analyze(merged)
        print(f"  [АНАЛИЗ]\n    {res}")
        out.append(f"**Анализ**\n```\n{res}\n```\n")

    with open("hybrid_result.md", "w", encoding="utf-8") as fp:
        fp.write("\n".join(out))
    print(f"\n{'='*64}\n→ Сохранено в hybrid_result.md")


if __name__ == "__main__":
    main()
