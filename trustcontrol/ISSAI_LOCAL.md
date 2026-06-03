# ISSAI локально на ПК — проверка качества казахского

Цель: проверить как ISSAI (`whisper-turbo-ksc2` от назарбаевских) распознаёт
казахский **бесплатно на своём ПК**, до того как платить за сервер.

Прод (Render) остаётся как есть — он просто начнёт обращаться к воркеру на твоём ПК
через бесплатный туннель. Когда качество устроит — перенесём воркер на VPS.

## Что нужно
- **Docker Desktop** (Windows/Mac): https://www.docker.com/products/docker-desktop
- В Docker Desktop → Settings → Resources выдели **5-6 GB RAM** (модель ест ~2.5GB).
- ПК должен быть **включён** пока тестируешь (туннель живёт пока работает контейнер).

## Запуск (3 шага)

### 1. Поднять воркер + туннель
Двойной клик по **`scripts/run-issai-local.bat`**
(или вручную из папки `trustcontrol`):
```bash
docker compose -f docker-compose.issai-local.yml up -d --build
```
Первый запуск качает модель ~1.5GB — подожди 3-5 минут.

### 2. Узнать адрес туннеля
Скрипт сам напечатает адрес. Если запускал вручную:
```bash
docker compose -f docker-compose.issai-local.yml logs cloudflared
```
Найди строку вида:
```
https://random-words-here.trycloudflare.com
```

### 3. Прописать в Render → Environment
```
ISSAI_WORKER_URL = https://random-words-here.trycloudflare.com
ISSAI_WORKER_KEY = trustlocal2026
```
Сохрани → дождись редеплоя → запиши казахский диалог.

В телеге `🔧 STT` теперь покажет `issai / ok` и нормальный казахский текст.

## Остановить
Двойной клик **`scripts/run-issai-stop.bat`** или:
```bash
docker compose -f docker-compose.issai-local.yml down
```
Модель остаётся в кэше — следующий запуск быстрый.

## Важно
- ⚠️ Адрес `trycloudflare.com` **меняется при каждом перезапуске** контейнера.
  Перезапустил — обнови `ISSAI_WORKER_URL` в Render.
- Это режим ПРОВЕРКИ. Когда качество устроит → переносим воркер на Hetzner VPS
  (~€4/мес), адрес станет постоянным, ПК держать включённым не надо.
- Проверить что воркер жив локально: открой http://localhost:8010/health
