# TrustControl — Руководство для разработчика и ИИ-ассистентов

## Суть проекта

SaaS-платформа для ИИ-мониторинга качества обслуживания.
Аналог системы "Петти" от Burger King — для малого бизнеса Казахстана.

**Как работает:**
1. USB-микрофон на кассе → скрипт `backend/worker/monitor.py`
2. Аудио отправляется POST-запросом на `/api/reports/submit`
3. Сервер транскрибирует через OpenAI Whisper
4. Анализирует фразы и тон через `backend/services/analyzer.py`
5. Сохраняет в БД и отправляет отчёт в Telegram

## Архитектура

```
main.py                  ← FastAPI app, регистрация роутов
backend/
  config.py              ← Настройки из .env (Settings class)
  database.py            ← SQLAlchemy engine, get_db, init_db
  __init__.py
  models/                ← SQLAlchemy ORM модели (таблицы БД)
    __init__.py          ← ВАЖНО: регистрирует все модели для init_db
    user.py              ← Владелец бизнеса
    location.py          ← Торговая точка (касса)
    report.py            ← Один разговор (транскрипция + флаги)
    alert.py             ← Тревога (грубость/мошенничество)
    shift.py             ← Статистика смены
    payment.py           ← Оплата подписки
  api/                   ← FastAPI роуты
    __init__.py
    auth.py              ← JWT авторизация + rate limiting
    locations.py         ← CRUD точек, проверка лимитов тарифа
    reports.py           ← Приём аудио от кассы, анализ
    alerts.py            ← Список тревог, resolve
    stats.py             ← Дашборд, графики за 7 дней
  services/              ← Бизнес-логика
    __init__.py
    whisper.py           ← OpenAI Whisper транскрипция
    analyzer.py          ← Анализ фраз + тон + мошенничество
    notifier.py          ← Telegram отчёты и тревоги
  worker/                ← Скрипт на кассовом ПК
    __init__.py
    monitor.py           ← PyAudio + VAD + отправка на сервер
frontend/
  dashboard/
    index.html           ← SPA дашборд (Vanilla JS + Chart.js)
```

## Ключевые паттерны

### Авторизация
- Пользователи (владельцы) авторизуются через JWT токен
- Кассовые скрипты авторизуются через `X-API-Key` заголовок
- Каждый endpoint проверяет что данные принадлежат текущему юзеру

### Безопасность
- Rate limit: 5 попыток логина / 60 сек (в памяти, per IP)
- Пароль: минимум 8 символов (Pydantic validator)
- Файлы: максимум 10MB на загрузку
- CORS: конкретные домены из `ALLOWED_ORIGINS` env
- Документация API (`/docs`) скрыта в продакшне

### Тарифы и лимиты
```python
limits = {"trial": 1, "start": 1, "business": 5, "network": 999}
```
Проверяются при создании новой точки в `api/locations.py`

### Типы бизнеса
```python
BUSINESS_TYPE ∈ {"coffee", "gas", "fastfood", "cafe", "beauty", "shop", "fitness", "hotel"}
```
Определяет набор бонусных фраз в `services/analyzer.py`

### Типы тревог
```python
alert_type ∈ {"fraud", "bad_language", "negative_tone", "no_greeting", "no_goodbye"}
```

### Смены
- Смена 1 (утро):  06:00 – 14:00
- Смена 2 (день):  14:00 – 22:00
- Смена 3 (ночь):  22:00 – 06:00

## Переменные окружения (обязательные)

```env
SECRET_KEY=<64+ символа>       # JWT подпись
OPENAI_API_KEY=sk-proj-...     # Whisper транскрипция
TELEGRAM_BOT_TOKEN=...         # Уведомления
DATABASE_URL=...               # SQLite или PostgreSQL
ALLOWED_ORIGINS=https://...    # CORS домены
```

## Что нужно доделать

- [ ] Alembic миграции (`alembic init` + первая миграция)
- [ ] Dockerfile для сборки образа
- [ ] nginx.conf для продакшна с SSL
- [ ] Email уведомления (SMTP) при мошенничестве
- [ ] Webhook Kaspi для автоподтверждения оплаты
- [ ] Страница аналитики по сотрудникам в дашборде
- [ ] Экспорт отчётов в Excel
- [ ] Тесты (pytest + pytest-asyncio)

## Команды для разработки

```bash
# Установка
pip install -r requirements.txt

# Запуск (dev)
DEBUG=true python main.py

# Запуск через Docker
docker-compose up -d

# Проверка API
curl http://localhost:8000/health
```

## Стек

| Слой | Технология |
|------|-----------|
| API | FastAPI + Uvicorn |
| БД | SQLAlchemy 2.0 async |
| БД dev | SQLite + aiosqlite |
| БД prod | PostgreSQL + asyncpg |
| Auth | JWT (python-jose) |
| ИИ | OpenAI Whisper |
| Уведомления | python-telegram-bot |
| Фронтенд | Vanilla JS + Chart.js |
| Деплой | Docker + docker-compose |
| Касса | PyAudio + WebRTC VAD |
