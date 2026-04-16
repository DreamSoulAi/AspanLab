# ════════════════════════════════════════════════════════════
#  Сервис: Анализ качества обслуживания через GPT-4o-mini
#  Вызывается после транскрипции, добавляет AI-резюме к отчёту
# ════════════════════════════════════════════════════════════

import json
import logging
from openai import AsyncOpenAI
from backend.config import settings

log = logging.getLogger("gpt_analyzer")
client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)

_SYSTEM_PROMPT = """Ты эксперт по качеству обслуживания клиентов в розничных магазинах и кафе Казахстана.
Тебе дают транскрипт разговора сотрудника с клиентом.
Проанализируй качество обслуживания и верни ТОЛЬКО валидный JSON без пояснений.

Формат ответа:
{
  "score": <число от 0 до 100>,
  "summary": "<одно предложение с итоговой оценкой>",
  "positives": ["<хорошее действие 1>", "<хорошее действие 2>"],
  "issues": ["<проблема 1>", "<проблема 2>"]
}

Критерии оценки:
- Приветствие (+15)
- Вежливость и уважение (+15)
- Помощь клиенту (+15)
- Предложение доп. товаров/услуг (+10)
- Прощание (+10)
- Грубость или безразличие (-25)
- Попытка мошенничества (-50)
- Негативный тон (-10)

Если текст слишком короткий или непонятный — верни score: 50, summary: "Недостаточно данных для анализа"."""


async def gpt_analyze(transcript: str) -> dict:
    """
    Анализирует транскрипт через GPT-4o-mini.
    Возвращает словарь: score, summary, positives, issues.
    При любой ошибке возвращает пустой результат.
    """
    if not settings.OPENAI_API_KEY:
        return {}

    if not transcript or len(transcript.strip()) < 10:
        return {}

    try:
        response = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user",   "content": f"Транскрипт разговора:\n\n{transcript}"},
            ],
            response_format={"type": "json_object"},
            max_tokens=300,
            temperature=0.2,
        )

        raw = response.choices[0].message.content
        result = json.loads(raw)

        # Нормализуем score в диапазон 0-100
        score = result.get("score", 50)
        result["score"] = max(0, min(100, int(score)))

        log.info(
            f"GPT анализ: score={result['score']} | "
            f"{result.get('summary', '')[:60]}"
        )
        return result

    except Exception as e:
        log.warning(f"GPT анализ недоступен: {e}")
        return {}
