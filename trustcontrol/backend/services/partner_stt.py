"""
partner_stt.py — клиент двух OpenAI-совместимых STT серверов партнёра.

Два сервера (порты 8000/8001) поднимаются партнёром на GPU-машине и пробрасываются
через Cloudflare Tunnel → стабильные URL без привязки к динамическому IP.

Включается когда заданы PARTNER_KK_URL + PARTNER_RU_URL. При отсутствии любого из
URL соответствующий кандидат считается недоступным → фолбэк на другой или gpt-4o.

Галлюцинации детектируются двухслойно:
  Слой 1 (бесплатно): loop-collapse ratio + TTR (type-token ratio).
    • Loop: _strip_repeat_loops схлопывает зацикливания; если после схлопывания
      осталось <60% токенов — это петля-галлюцинация (тест2-KK: біріншін×40).
    • TTR: доля уникальных слов. Нормальная речь >0.40, каша ~0.08 (тест3-RU: салас×50).
    Порог TTR 0.25 даёт хороший запас: самый «повторный» нормальный текст в тестах ~0.42.
  Слой 2 (gpt-4o-mini, ~$0.0001): когда ОБА кандидата прошли слой 1 —
    mini смотрит оба и возвращает какой связный (ловит иноязычную кашу без повторов,
    как тест1-RU: 47 слов разных, но бред). Это НЕ лишний вызов — тот же mini-проход
    что делает reconstruct/merge, просто кормим ему двух кандидатов.

Возможные исходы _pick_best_candidate():
  "kk"        — KK партнёр прошёл, RU сломан/недоступен
  "ru"        — RU партнёр прошёл, KK сломан/недоступен
  "mini_kk"   — оба чистые, mini выбрал KK
  "mini_ru"   — оба чистые, mini выбрал RU
  None        — оба сломаны/недоступны → фолбэк gpt-4o в audio_analyzer
"""

import asyncio
import io
import logging
import os
import re

log = logging.getLogger(__name__)

PARTNER_KK_URL   = os.getenv("PARTNER_KK_URL",   "")
PARTNER_KK_MODEL = os.getenv("PARTNER_KK_MODEL", "shyngys879/kazakh-whisper-large-v3-turbo")
PARTNER_KK_LANG  = os.getenv("PARTNER_KK_LANG",  "kk")

PARTNER_RU_URL   = os.getenv("PARTNER_RU_URL",   "")
PARTNER_RU_MODEL = os.getenv("PARTNER_RU_MODEL", "coriollon/whisper-large-v3-turbo-russian")
PARTNER_RU_LANG  = os.getenv("PARTNER_RU_LANG",  "ru")

PARTNER_TIMEOUT  = float(os.getenv("PARTNER_STT_TIMEOUT", "30"))

# Детект галлюцинаций
_LOOP_KEEP_RATIO = float(os.getenv("PARTNER_LOOP_KEEP_RATIO", "0.60"))
_TTR_THRESHOLD   = float(os.getenv("PARTNER_TTR_THRESHOLD",   "0.25"))
_MIN_WORDS       = int(os.getenv("PARTNER_MIN_WORDS",         "3"))


def is_enabled() -> bool:
    return bool(PARTNER_KK_URL and PARTNER_RU_URL)


# ── Слой 1: детерминированный детектор галлюцинаций ──────────────────────────

def _strip_repeat_loops(text: str) -> str:
    """Схлопывает 3+ подряд идущих повтора фразы (1-3 слова)."""
    if not text:
        return text
    tokens = text.split()
    if len(tokens) < 6:
        return text

    def _norm(t: str) -> str:
        return re.sub(r"\W+", "", t.lower(), flags=re.UNICODE)

    out: list[str] = []
    i, n = 0, len(tokens)
    while i < n:
        collapsed = False
        for plen in (1, 2, 3):
            if i + plen > n:
                continue
            phrase = [_norm(t) for t in tokens[i:i + plen]]
            if not any(phrase):
                continue
            reps, j = 1, i + plen
            while j + plen <= n and [_norm(t) for t in tokens[j:j + plen]] == phrase:
                reps += 1
                j += plen
            if reps >= 3:
                out.extend(tokens[i:i + plen])
                i = j
                collapsed = True
                break
        if not collapsed:
            out.append(tokens[i])
            i += 1
    return " ".join(out)


def _ttr(text: str) -> float:
    """Type-Token Ratio: доля уникальных слов. Норм. речь >0.40, каша ~0.08."""
    tokens = [re.sub(r"\W+", "", t.lower()) for t in text.split() if t.strip()]
    tokens = [t for t in tokens if t]
    if not tokens:
        return 0.0
    return len(set(tokens)) / len(tokens)


def _hallucination_layer1(raw: str) -> tuple[bool, str]:
    """
    Возвращает (is_hallucination, reason).
    True → текст сломан, использовать нельзя.
    """
    if not raw or len(raw.split()) < _MIN_WORDS:
        return True, "empty_or_too_short"

    stripped = _strip_repeat_loops(raw)
    orig_tokens = len(raw.split())
    kept_tokens = len(stripped.split())
    loop_ratio = kept_tokens / orig_tokens if orig_tokens else 0

    if loop_ratio < _LOOP_KEEP_RATIO:
        return True, f"loop_collapse ratio={loop_ratio:.2f} (порог {_LOOP_KEEP_RATIO})"

    ttr = _ttr(stripped)
    if ttr < _TTR_THRESHOLD:
        return True, f"low_TTR={ttr:.2f} (порог {_TTR_THRESHOLD})"

    return False, f"ok loop_ratio={loop_ratio:.2f} TTR={ttr:.2f}"


# ── Слой 2: mini-судья когда оба кандидата прошли слой 1 ─────────────────────

async def _mini_judge(kk_text: str, ru_text: str) -> str:
    """
    Просит gpt-4o-mini выбрать какой из двух транскриптов связный.
    Возвращает "kk" или "ru". При ошибке → "kk" (KK лучше на смеси).
    """
    from backend.services.audio_analyzer import client as _oai

    prompt = (
        "Тебе два транскрипта одной аудиозаписи с кассы, сделанных разными моделями. "
        "Один может быть галлюцинацией (бессмысленный набор слов / иноязычная каша). "
        "Ответь ТОЛЬКО одним словом: 'kk' если первый транскрипт связнее, 'ru' если второй.\n\n"
        f"ТРАНСКРИПТ 1 (kk):\n{kk_text[:400]}\n\n"
        f"ТРАНСКРИПТ 2 (ru):\n{ru_text[:400]}"
    )
    try:
        resp = await _oai.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=5,
            temperature=0,
        )
        answer = (resp.choices[0].message.content or "").strip().lower()
        return "ru" if "ru" in answer else "kk"
    except Exception as e:
        log.warning(f"partner_stt mini-судья ошибка: {e} → выбираем kk")
        return "kk"


# ── Транскрипция через один партнёрский сервер ───────────────────────────────

async def _transcribe_one(wav_bytes: bytes, url: str, model: str, lang: str) -> str:
    """
    Отправляет WAV на OpenAI-совместимый endpoint партнёра.
    При ошибке/таймауте бросает исключение (обрабатывается выше).
    """
    from openai import AsyncOpenAI
    cli = AsyncOpenAI(api_key="not-needed", base_url=url, timeout=PARTNER_TIMEOUT, max_retries=0)
    buf = io.BytesIO(wav_bytes)
    buf.name = "audio.wav"
    tr = await cli.audio.transcriptions.create(model=model, file=buf, language=lang)
    return (getattr(tr, "text", "") or "").strip()


# ── Главная функция: параллельный прогон + выбор победителя ─────────────────

async def transcribe(wav_bytes: bytes) -> tuple[str | None, str]:
    """
    Запускает KK и RU параллельно, детектирует галлюцинации, выбирает лучший.

    Возвращает (text, engine):
      text   — итоговый транскрипт (или None если оба сломаны)
      engine — "partner_kk" / "partner_ru" / "partner_mini_kk" / "partner_mini_ru" /
               "partner_both_failed"
    """
    async def _safe_transcribe(url, model, lang, label):
        try:
            text = await _transcribe_one(wav_bytes, url, model, lang)
            broken, reason = _hallucination_layer1(text)
            log.info(f"partner_stt [{label}]: {len(text.split())} слов | layer1={'broken' if broken else 'ok'} | {reason}")
            return text, broken
        except Exception as e:
            log.warning(f"partner_stt [{label}] ошибка: {type(e).__name__}: {str(e)[:160]}")
            return "", True

    kk_task = asyncio.create_task(_safe_transcribe(PARTNER_KK_URL, PARTNER_KK_MODEL, PARTNER_KK_LANG, "kk"))
    ru_task = asyncio.create_task(_safe_transcribe(PARTNER_RU_URL, PARTNER_RU_MODEL, PARTNER_RU_LANG, "ru"))

    (kk_text, kk_broken), (ru_text, ru_broken) = await asyncio.gather(kk_task, ru_task)

    if kk_broken and ru_broken:
        log.warning("partner_stt: оба кандидата сломаны → фолбэк gpt-4o")
        return None, "partner_both_failed"

    if not kk_broken and ru_broken:
        return kk_text, "partner_kk"

    if kk_broken and not ru_broken:
        return ru_text, "partner_ru"

    # Оба прошли слой 1 → mini-судья (слой 2)
    winner = await _mini_judge(kk_text, ru_text)
    text = kk_text if winner == "kk" else ru_text
    engine = f"partner_mini_{winner}"
    log.info(f"partner_stt mini-судья выбрал: {winner} ({len(text.split())} слов)")
    return text, engine
