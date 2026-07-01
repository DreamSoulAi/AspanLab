# TODO_PROD.md — TrustControl

_Обновлено: 01.07.2026. Сервер: 5.63.112.52 (новый), старый 213.155.21.25 — горячий резерв._

---

## СЕКЦИЯ 1: Блокеры пилота

> Должно быть закрыто до показа первому платящему клиенту.

### Критические

- [ ] **Smoke test: Telegram-уведомления об OK-разговорах** — в smoke test не пришли.
  Диагностика: `sudo docker logs --since 30m trustcontrol_api 2>&1 | grep -v subscription_reminder | tail -80`
  Вероятные причины: (а) тест-текст < 10 символов → `gpt_analyze` отбрасывает без вызова OpenAI;
  (б) GPT классифицирует как IGNORE (текст слишком короткий/без явного диалога);
  (в) `notify_ok_conversations=false` (проверить: `SELECT id, name, notify_ok_conversations FROM locations WHERE id=1`).
  Тест: `curl -X POST https://trustcontrol.kz/api/reports/submit -H "X-API-Key: <ключ>" -F "transcript_text=Здравствуйте, один американо пожалуйста, 700 тенге, спасибо"`

- [ ] **Smoke test: аудио-плеер R2** — открыть отчёт в дашборде, нажать «Слушать запись».
  Если кнопки нет — у отчёта нет `s3_key` (audio пришло через `transcript_text`, не через WAV).
  Тест с реальным аудио нужен отдельно.

- [ ] **Smoke test: партнёрский ISSAI (10.221.0.228:8000/v1)** — когда партнёр подтвердит модели.
  Проверить `curl http://10.221.0.228:8010/health` из контейнера api, затем послать WAV и убедиться
  что в логах `engine=parallel_hybrid` или `engine=issai_cascade`.

- [ ] **Smoke test: KASPI_FRAUD end-to-end** — отправить диалог с фразой перевода на личный номер.
  Ожидание: тревога KASPI_FRAUD в Telegram + кнопка «Слушать». Без `allowed_phones` → KASPI_UNVERIFIED (LOW).
  Тест-скрипт: `python test_kaspi_15.py` (требует SECRET_KEY + DATABASE_URL + OPENAI_API_KEY).

### Инфраструктура

- [ ] **Убрать subscription_reminder спам** — user=3 заблокировал бота, Exception каждую минуту
  топит логи. Найти и удалить/починить:
  ```sql
  SELECT id, email, telegram_chat FROM users WHERE id=3;
  UPDATE users SET telegram_chat=NULL WHERE id=3;
  ```

- [ ] **Проверить backup cron** — убедиться что бэкапы идут:
  ```
  crontab -l          # показать задачи root
  ls -lh /root/backups/   # последний файл
  ```

- [ ] **Отключить старый VPS 213.155.21.25** — через 5 дней после стабилизации нового сервера.
  Перед выключением убедиться: DNS trustcontrol.kz → 5.63.112.52, бот BotFather → новый сервер.

- [ ] **Обновить CLAUDE.md** — IP изменился с 213.155.21.25 на 5.63.112.52.
  Поле «ИНФРАСТРУКТУРА ПРОДА» и раздел ISSAI воркера.

---

## СЕКЦИЯ 2: Блокеры публичного запуска

> Перед маркетингом и масштабированием (>1 клиента).

### Безопасность

- [ ] **Проверить закрытость порта 8000** — в docker-compose.prod.yml `expose: ["8000"]` (не `ports:`),
  но smoke test показал его видимым снаружи. Проверить:
  ```
  ss -tlnp | grep 8000   # должен быть 0.0.0.0:8000 ТОЛЬКО внутри Docker-сети, не на хосте
  ```
  Если порт открыт на хосте: добавить в UFW `ufw deny 8000/tcp` или проверить docker network.

- [ ] **Отключить OTP bypass** — `.env.prod` сейчас `OTP_BYPASS=true` (вход без кода).
  Перед публичным запуском: `OTP_BYPASS=false` + проверить что SMS/Telegram-код реально приходит.

- [ ] **HTML escape в email-шаблонах** — `send_fraud_email()` в `notifier.py` вставляет
  `location_name` и `description` в HTML без экранирования. Если кто-то назовёт точку как
  `<script>...</script>` — HTML в письме сломается. Добавить `html.escape()` для всех полей.

- [ ] **Убрать трейсбек из Telegram-ошибок** — `_err_text = _tb.format_exc()[-800:]`
  отправляется в чат владельца. Если там окажется чувствительная информация (ключи, SQL) —
  утечка. Заменить на краткое сообщение, детали — только в логи.

### Функциональность

- [ ] **Email при KASPI_FRAUD** — `email_sender.py` и `send_fraud_email()` есть, но SMTP-сервис
  (Resend/Gmail) не настроен в `.env.prod`. Без email владелец узнаёт о фроде только из Telegram.
  Выставить `RESEND_API_KEY` или SMTP-параметры.

- [ ] **Kaspi webhook** — автоподтверждение оплаты: Kaspi Pay → `/api/webhook/kaspi` → активация
  подписки. Сейчас вручную через `/auth/admin/extend-subscription`. Без автоплатёжки не масштабируется.

- [ ] **Экспорт в Excel** — упоминается в CLAUDE.md как backlog, но клиенты КЗ ожидают Excel-выгрузку.
  Минимальная версия: GET `/api/reports/export?location_id=&start=&end=` → `.xlsx` (openpyxl).

- [ ] **Обработчик 404/500** — сейчас FastAPI возвращает JSON с деталями Python-ошибки.
  Добавить кастомные `@app.exception_handler` (404 → редирект на дашборд, 500 → краткое сообщение).

### Операционность

- [ ] **Ротация Docker-логов** — добавить в docker-compose.prod.yml:
  ```yaml
  logging:
    driver: "json-file"
    options:
      max-size: "50m"
      max-file: "5"
  ```
  Без этого логи разрастаются до нескольких GB и кончается место на диске.

- [ ] **Внешний мониторинг** — нет ping-check снаружи. Если упадёт nginx или api-контейнер,
  узнаем только от клиента. Настроить https://healthchecks.io или UptimeRobot на `/health`.

- [ ] **Проверить конкурентность под нагрузкой** — 5 точек одновременно = 5 фоновых задач
  по ~10-90с каждая. `MAX_CONCURRENT_PROCESSING=5` + Neon free tier = 5 соединений.
  Пиковый тест: 5 параллельных curl с WAV → все должны завершиться без QueuePool Timeout.

---

## СЕКЦИЯ 3: Бэклог

> После стабилизации прода, при росте >10 точек.

### STT и распознавание

- [ ] **Soyle API** (soyle.ai) — облачный казахский STT как альтернатива ISSAI при недоступности VPS.
- [ ] **Расширить `_PHONE_RE`** — не ловит «7071234567» (10 цифр без «8»/«+7»). KNOWN LIMITATION.
  Вернуться когда появится реальный кейс фрода с таким форматом.
- [ ] **faster-whisper base в monitor.py** — лёгкий on-device пре-фильтр тишины до отправки на сервер
  (экономия трафика и OpenAI). Актуально при >15 точках.

### Клиентские фичи

- [ ] **Страница аналитики по сотрудникам** — `energy_level` и имена уже в БД, дашборда нет.
- [ ] **Экспорт истории в Excel** — сохранение за произвольный период.
- [ ] **Настройка email-уведомлений** в ЛК (сейчас только Telegram).
- [ ] **Webhook Kaspi Pay** — автооплата без ручного `/extend-subscription`.

### Кассовый клиент

- [ ] **Raspberry Pi headless** — `scripts/raspberry/` написан, не протестирован на железе.
- [ ] **Windows 7 EXE** — совместимость упоминается в CLAUDE.md, но не было финального smoke test
  на реальном Win7.
- [ ] **Offline-буфер .exe** — при потере сети `_save_fail` в `fails/`, но не тестировался
  в реальных условиях обрыва 3G/Wi-Fi.

### Маркетинг и продажи

- [ ] **Threads Bot** — `marketing/threads_bot.py` написан, ждёт токенов Meta (THREADS_USER_ID +
  ACCESS_TOKEN). Настройка: `marketing/THREADS_BOT_SETUP.md`.
- [ ] **Реферальная программа** — код и API есть, размер бонуса не определён. Решить после первого
  оплаченного месяца.

### Техдолг

- [ ] **Тесты (pytest + pytest-asyncio)** — критично для CI/CD, сейчас нет автоматических тестов
  для интеграционных сценариев.
- [ ] **Alembic check в CI** — `alembic check` в GitHub Actions, блокирует PR если модель
  расходится с миграциями.
- [ ] **POS-матчер** — сверка транзакций Kaspi с репортами (нужен фид чеков от клиента, QR-scan).
  Backlog, пока нет фида.
- [ ] **Аудит-лог admin-действий** — `/admin/extend-subscription` и другие не пишут в БД;
  сложно ретроспективно проверить кто что менял.
