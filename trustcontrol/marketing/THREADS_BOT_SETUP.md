# Автопостинг в Threads — настройка

Бот `threads_bot.py` сам придумывает тему, пишет пост в твоём стиле и публикует
в Threads. Темы руками писать не надо. По умолчанию — режим **вето**: за час до
публикации шлёт черновик тебе в Telegram с кнопками. Не тронул → публикует сам.

Крутить на **VPS** (всегда включён), не на Render (free-tier засыпает).

---

## Что нужно получить (15-20 минут, один раз)

### 1. Токен Threads API (Meta)
1. Зайди на https://developers.facebook.com → **My Apps** → **Create App**.
2. Тип приложения — выбери с доступом к **Threads API** (Use case: Threads).
3. В приложении подключи свой Threads-аккаунт, выдай разрешения
   `threads_basic`, `threads_content_publish`.
4. Сгенерируй **долгоживущий access token** (long-lived, ~60 дней, продлевается).
5. Узнай свой **Threads user id** (в Graph API Explorer: `GET /me?fields=id`).
   → это `THREADS_USER_ID` и `THREADS_ACCESS_TOKEN`.

Док: https://developers.facebook.com/docs/threads

### 2. Второй Telegram-бот (для кнопки вето)
Прод-бот сидит на webhook — на нём вето работать не может. Нужен отдельный:
1. Напиши @BotFather → `/newbot` → назови «TrustControl Content».
2. Получишь токен → это `THREADS_TG_TOKEN`.
3. Напиши этому новому боту любое сообщение (чтобы он мог тебе писать).
4. Свой `chat_id` узнай: открой
   `https://api.telegram.org/bot<ТОКЕН>/getUpdates` после сообщения боту —
   там будет `chat.id`. → это `THREADS_TG_CHAT`.

---

## Переменные окружения (на VPS, в `marketing/.env`)

```
OPENAI_API_KEY=sk-...
THREADS_USER_ID=...
THREADS_ACCESS_TOKEN=...
THREADS_TG_TOKEN=...
THREADS_TG_CHAT=...
THREADS_MODE=veto
THREADS_HOLD_MIN=60
THREADS_POST_HOUR_UTC=15      # 15 UTC = 20:00 Алматы (пик Threads)
```

---

## Проверка и запуск

Сгенерить пост и посмотреть, ничего не публикуя:
```
python threads_bot.py --dry
```

Один цикл прямо сейчас (с учётом режима):
```
python threads_bot.py --once
```

### Вариант А — демон (сам ждёт 20:00 каждый день)
Создай `/etc/systemd/system/threads-bot.service`:
```
[Unit]
Description=TrustControl Threads bot
After=network.target

[Service]
WorkingDirectory=/root/AspanLab/trustcontrol/marketing
ExecStart=/usr/bin/python3 threads_bot.py --loop
Restart=always

[Install]
WantedBy=multi-user.target
```
Затем:
```
systemctl enable --now threads-bot
```

### Вариант Б — cron (проще, без демона), каждый день 14:00 UTC = 19:00 Алматы
В режиме `veto` черновик придёт в 19:00, опубликуется в ~20:00 (или раньше — по кнопке):
```
0 14 * * * cd /root/AspanLab/trustcontrol/marketing && /usr/bin/python3 threads_bot.py --once >> threads.log 2>&1
```

---

## Режимы (env `THREADS_MODE`)

| Режим   | Что делает |
|---------|-----------|
| `veto`  | (по умолч.) шлёт черновик в Telegram, ждёт `THREADS_HOLD_MIN` мин. Не тронул → публикует. Кнопки: ✅ опубликовать сейчас / 🔁 другой пост / ❌ пропустить. |
| `auto`  | публикует сразу, без спроса. |
| `draft` | только генерит и шлёт в Telegram, НЕ публикует (постишь руками). |

## Частота
1 пост/день, вечер. Не чаще — алгоритм Threads режет охваты у частящих новых
аккаунтов. Через 2-3 недели, когда пойдёт отклик, можно добавить утренний слот.

## Банк тем
Угол берётся из `ANGLES` в `threads_bot.py` (наживки / боль / позиционирование /
крючки на статьи). Бот выбирает тот, что дольше всего не выходил, и не повторяет
формулировки последних 6 постов. Хочешь добавить тему — допиши строку в `ANGLES`.
Журнал опубликованного — `threads_posted.log`.
