# TrustControl — Документация для разработчика

## Структура проекта

```
trustcontrol/
│
├── main.py                        ← точка входа FastAPI сервера
├── requirements.txt               ← зависимости сервера
├── requirements-monitor.txt       ← зависимости скрипта кассы
├── docker-compose.yml             ← деплой одной командой
│
├── backend/
│   ├── config.py                  ← все настройки (env переменные)
│   ├── database.py                ← SQLAlchemy engine + get_db
│   │
│   ├── models/                    ← SQLAlchemy модели (таблицы БД)
│   │   ├── user.py                ← владелец бизнеса
│   │   ├── location.py            ← торговая точка (касса)
│   │   ├── report.py              ← каждый разговор
│   │   ├── alert.py               ← тревоги (грубость, мошенничество)
│   │   ├── shift.py               ← статистика смены
│   │   └── payment.py             ← оплата подписки
│   │
│   ├── api/                       ← FastAPI роуты
│   │   ├── auth.py                ← /api/auth/* (логин, регистрация, JWT)
│   │   ├── locations.py           ← /api/locations/* (CRUD точек)
│   │   ├── reports.py             ← /api/reports/* (приём аудио, список)
│   │   ├── alerts.py              ← /api/alerts/* (тревоги)
│   │   └── stats.py               ← /api/stats/* (дашборд, графики)
│   │
│   ├── services/                  ← бизнес-логика
│   │   ├── whisper.py             ← транскрипция через OpenAI Whisper
│   │   ├── analyzer.py            ← анализ фраз + тона
│   │   └── notifier.py            ← Telegram уведомления
│   │
│   └── worker/
│       └── monitor.py             ← скрипт для кассового ПК
│
└── frontend/
    └── dashboard/
        └── index.html             ← веб-дашборд (SPA на ванильном JS)
```

---

## Как работает система целиком

```
Касса (monitor.py)
    │
    │  POST /api/reports/submit
    │  + WAV аудио + X-API-Key заголовок
    ▼
FastAPI сервер (main.py)
    │
    ├── Whisper API → транскрипция текста
    ├── analyzer.py → поиск фраз + тон
    ├── Сохранение в PostgreSQL
    └── Telegram уведомление → владельцу
    │
    ▼
Веб-дашборд (index.html)
    │
    └── Получает данные через /api/stats/dashboard
```

---

## Быстрый старт для разработчика

### 1. Клонируй и настрой

```bash
git clone <repo>
cd trustcontrol

# Создай .env файл
cp .env.example .env
# Заполни .env своими ключами
```

### 2. Запуск через Docker (рекомендуется)

```bash
docker-compose up -d
```

Сервер будет на http://localhost:8000
Дашборд будет на http://localhost:80

### 3. Запуск локально (для разработки)

```bash
# Установи зависимости
pip install -r requirements.txt

# Запусти сервер
python main.py
```

### 4. Скрипт на кассе

```bash
# На кассовом ПК (Windows/Mac)
pip install -r requirements-monitor.txt

# Заполни SERVER_URL и API_KEY в monitor.py
python backend/worker/monitor.py
```

---

## API Endpoints

| Метод | URL | Описание |
|-------|-----|----------|
| POST | /api/auth/register | Регистрация |
| POST | /api/auth/login | Логин → JWT токен |
| GET  | /api/auth/me | Текущий пользователь |
| GET  | /api/locations/ | Список точек |
| POST | /api/locations/ | Создать точку |
| POST | /api/reports/submit | Скрипт кассы отправляет аудио |
| GET  | /api/reports/ | Список отчётов |
| GET  | /api/alerts/ | Список тревог |
| PATCH | /api/alerts/{id}/resolve | Пометить тревогу решённой |
| GET  | /api/stats/dashboard | Статистика для дашборда |

Полная документация API: http://localhost:8000/docs

---

## База данных

### Схема

```
users ──────────────── locations ─── reports
  │                        │             │
  └── payments             │             └── alerts
                           ├── alerts
                           └── shifts
```

### Миграции через Alembic

```bash
# Инициализация
alembic init database/migrations

# Создать миграцию
alembic revision --autogenerate -m "Initial"

# Применить
alembic upgrade head
```

---

## Тарифы и ограничения

| Тариф | Точек | Цена |
|-------|-------|------|
| trial | 1 | Бесплатно 14 дней |
| start | 1 | 9 900 ₸/мес |
| business | 5 | 24 900 ₸/мес |
| network | ∞ | По запросу |

Ограничения проверяются в `/api/locations/ POST`

---

## Что нужно доделать программисту

- [ ] Alembic миграции (alembic init + первая миграция)
- [ ] .env файл с реальными ключами
- [ ] nginx.conf для продакшна
- [ ] SSL сертификат (Let's Encrypt)
- [ ] Dockerfile для сборки образа
- [ ] Смена статистики смен (сейчас считается on-the-fly)
- [ ] Email уведомления при мошенничестве
- [ ] Экспорт в Excel из дашборда
- [ ] Страница аналитики по сотрудникам
- [ ] Kaspi webhook для автоподтверждения оплаты

---

## Стек технологий

- **Backend:** Python 3.11, FastAPI, SQLAlchemy 2.0
- **БД:** SQLite (dev) → PostgreSQL (prod)
- **Auth:** JWT (python-jose)
- **ИИ:** OpenAI Whisper (транскрипция)
- **Уведомления:** python-telegram-bot
- **Фронтенд:** Vanilla JS + Chart.js (без фреймворков)
- **Деплой:** Docker + docker-compose + nginx
- **Касса:** PyAudio + WebRTC VAD + noisereduce
