# TrustControl — Руководство для разработчика и ИИ-ассистентов

> **ДЛЯ ИИ-АССИСТЕНТА: читай этот раздел первым делом в каждой новой сессии.**

## Контекст сессии — читать обязательно

**Владелец проекта:** Данил, Казахстан. Строит SaaS с нуля, без команды.
**Главная боль прямо сейчас:** продукт задеплоен, но плохо понимает казахский и шала-казахский язык — именно это мешает получить первых клиентов.
**Приоритет:** сделать чтобы казахский диалог нормально распознавался и анализировался. Всё остальное — второстепенно.

### Как работать с Данилом

- Он часто присылает **скрин без текста** — это скрин ошибки или дашборда. Смотри на скрин, анализируй сам, не проси объяснений.
- Если прислал **скрин логов** — читай логи, находи ошибку, предлагай фикс.
- Если прислал **скрин дашборда** — смотри что не так (пустые данные, 0 в полях, неправильный текст), объясняй причину.
- Не задавай лишних вопросов — Данил устал от переписок. Действуй, потом уточняй если реально нужно.
- Всегда пушить в **main** ветку (его постоянная инструкция).
- Не добавляй фичи сверх задачи. Не рефакторь без просьбы.

### Текущий статус проекта (май 2026)

**Что работает:**
- Авторизация, дашборд, Telegram-уведомления
- Запись аудио через PWA (браузер на кассе)
- Анализ через GPT-4o-mini-audio-preview (тон, грубость, fraud)
- Аналитика по сотрудникам с energy_level (1-5)
- Гибкие смены (дневная/ночная, владелец настраивает время)
- Win7-совместимая сборка .exe для кассового ПК

**Что НЕ работает / главная проблема:**
- Казахский и шала-казахский плохо распознаётся через OpenAI
- Реальные тесты с казахскими диалогами дают плохой результат
- Клиентов пока нет — именно из-за языковой проблемы

**Решение которое реализовано в коде (но ещё не включено в проде):**
Цепочка STT: **ISSAI (self-hosted) → Yandex SpeechKit → OpenAI аудио-модель**
- `backend/services/issai_stt.py` — клиент к self-hosted воркеру
- `backend/worker/issai_worker.py` — FastAPI сервер с faster-whisper (whisper-turbo-ksc2)
- `backend/services/yandex_stt.py` — Yandex SpeechKit клиент
- Ключи Yandex (`YANDEX_STT_API_KEY`, `YANDEX_STT_FOLDER_ID`) в проде **не выставлены**
- Это главный незакрытый шаг для решения казахской проблемы

**Env-переменные которые нужно добавить в прод для казахского:**
```
YANDEX_STT_API_KEY=...        # из Yandex Cloud → IAM → сервисный аккаунт
YANDEX_STT_FOLDER_ID=...      # id каталога в Yandex Cloud
YANDEX_STT_LANG=kk-KZ
```
Или для ISSAI воркера (self-hosted, дороже в настройке но бесплатно потом):
```
ISSAI_WORKER_URL=http://vps-ip:8010
ISSAI_WORKER_KEY=секрет
```

---

## Суть проекта

SaaS-платформа для ИИ-мониторинга качества обслуживания.
Аналог системы "Петти" от Burger King — для малого бизнеса Казахстана.

**Как работает:**
1. Микрофон на кассе → PWA в браузере (`frontend/mic/index.html`)
2. Аудио отправляется POST-запросом на `/api/reports/submit` — **целый разговор одним файлом** (не кусками по 30с)
3. Сервер распознаёт речь: ISSAI → Yandex → OpenAI Whisper (фолбэк)
4. Анализирует тон и бизнес-события через `gpt-4o-mini-audio-preview`
5. Сохраняет в БД и отправляет отчёт в Telegram

## Архитектура

```
main.py                        ← FastAPI app, регистрация роутов, _fix_schema()
backend/
  config.py                    ← Настройки из .env (Settings class)
  database.py                  ← SQLAlchemy engine, get_db, init_db
  models/
    user.py                    ← Владелец бизнеса
    location.py                ← Торговая точка (касса)
    report.py                  ← Один разговор (транскрипция + флаги + energy_level)
    alert.py                   ← Тревога
    shift.py                   ← Статистика смены
    payment.py                 ← Оплата подписки
  api/
    auth.py                    ← JWT + rate limiting
    locations.py               ← CRUD точек
    reports.py                 ← Приём аудио, анализ
    alerts.py                  ← Список тревог
    stats.py                   ← Дашборд, графики, аналитика сотрудников
  services/
    audio_analyzer.py          ← ГЛАВНЫЙ сервис: цепочка STT + анализ тона
    issai_stt.py               ← ISSAI self-hosted STT клиент
    yandex_stt.py              ← Yandex SpeechKit STT клиент
    gpt_analyzer.py            ← Текстовый анализ через gpt-4o-mini
    whisper.py                 ← Fallback транскрипция
    notifier.py                ← Telegram
    context_analyzer.py        ← Определение что это рабочий разговор
    employee_matcher.py        ← Сопоставление имён сотрудников
  worker/
    monitor.py                 ← PyAudio + VAD + отправка (кассовый ПК)
    issai_worker.py            ← FastAPI inference сервер (faster-whisper)
    requirements-issai.txt     ← Зависимости воркера
frontend/
  dashboard/index.html         ← SPA дашборд
  mic/index.html               ← PWA запись аудио на кассе
Dockerfile.issai               ← Docker образ для ISSAI воркера
docker-compose.issai.yml       ← Запуск ISSAI воркера
```

## Ключевые архитектурные решения

### STT цепочка (audio_analyzer.py)
```
аудио → ISSAI (если ISSAI_WORKER_URL задан)
      → Yandex SpeechKit (если YANDEX_STT_API_KEY + FOLDER_ID заданы)
      → gpt-4o-mini-audio-preview (всегда, основной анализ тона)
      → Whisper-1 (fallback если аудио-модель не вернула транскрипт)
```
Yandex/ISSAI дают точные казахские СЛОВА → аудио-модель оценивает ТОН по звуку.

### PWA запись
- Весь разговор = один файл (не кусками по 30с)
- Авто-сброс через 180 сек (защита лимита 10MB)
- Формат WAV 16kHz моно

### Авторизация
- Владельцы → JWT токен
- Кассовые скрипты → `X-API-Key` заголовок

### Тарифы
```python
limits = {"trial": 1, "start": 1, "business": 3, "potok": 5, "network": 999}
```

### Смены (настраиваются владельцем)
- Дневная: владелец задаёт start/end в UTC+5
- Ночная: владелец задаёт start/end в UTC+5
- Kazakhstan timezone = UTC+5

### energy_level (1-5)
Вовлечённость кассира: 5=живой энергичный, 1=мёртвый роботичный.
Поле в `Report.energy_level`, отображается в аналитике сотрудников.

## Переменные окружения

```env
# Обязательные
SECRET_KEY=<64+ символа>
OPENAI_API_KEY=sk-proj-...
TELEGRAM_BOT_TOKEN=...
DATABASE_URL=sqlite+aiosqlite:///trustcontrol.db
ALLOWED_ORIGINS=https://yourdomain.com

# Для казахского (НУЖНО ВЫСТАВИТЬ В ПРОДЕ — это главная незакрытая задача)
YANDEX_STT_API_KEY=         ← Yandex Cloud → IAM → сервисный аккаунт → API-ключ
YANDEX_STT_FOLDER_ID=       ← id каталога (в URL консоли)
YANDEX_STT_LANG=kk-KZ

# Или self-hosted (дороже в настройке)
ISSAI_WORKER_URL=            ← http://vps:8010
ISSAI_WORKER_KEY=

# Опциональные
ADMIN_PHONE=+7...
KASPI_NUMBER=+7...
KASPI_NAME=Данил Т.
OTP_BYPASS=false
```

## Стек

| Слой | Технология |
|------|-----------|
| API | FastAPI + Uvicorn |
| БД | SQLAlchemy 2.0 async |
| БД dev | SQLite + aiosqlite |
| БД prod | PostgreSQL + asyncpg |
| Auth | JWT (python-jose) |
| STT | ISSAI → Yandex SpeechKit → OpenAI |
| Анализ тона | gpt-4o-mini-audio-preview |
| Уведомления | python-telegram-bot |
| Фронтенд | Vanilla JS + Chart.js |
| Деплой | Docker + docker-compose |
| Касса | PyAudio + WebRTC VAD |

## Команды

```bash
pip install -r requirements.txt
DEBUG=true python main.py
curl http://localhost:8000/health
```

## Что нужно доделать (по приоритету)

- [ ] **ГЛАВНОЕ:** выставить Yandex STT ключи в прод и проверить казахский диалог
- [ ] Найти первого клиента (кафе/магазин в Казахстане) — дать бесплатно на месяц
- [ ] Alembic миграции
- [ ] Email уведомления при мошенничестве
- [ ] Webhook Kaspi для автоподтверждения оплаты
- [ ] Экспорт отчётов в Excel
- [ ] Тесты (pytest + pytest-asyncio)
