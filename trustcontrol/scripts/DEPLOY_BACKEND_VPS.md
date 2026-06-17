# Деплой бэкенда TrustControl на свой VPS (ps.kz, Алматы)

Бэкенд переезжает с Render на твой VPS (там же где ISSAI-воркер).
БД остаётся на Neon (данные уже там). Домен `trustcontrol.kz`.

> ⚠️ Память: VPS 4GB, ISSAI ест ~2.5–3.5GB. Лимит api=1.2G + swap.
> Если api начнёт падать с OOM — поднять RAM на VPS или вынести API отдельно.

---

## ПЕРЕД НАЧАЛОМ — переключить DNS

В панели домена `trustcontrol.kz` запись **A** должна указывать на IP VPS
`213.155.21.25` (вместо Render). Без этого SSL не выпустится.
Проверка (с любого компа): `nslookup trustcontrol.kz` → должен показать 213.155.21.25.

---

## Команды на VPS (по одной, заходишь: ssh ubuntu@213.155.21.25 → sudo -i)

### 1. Перейти в репозиторий и обновить main
```
cd /home/ubuntu/AspanLab/trustcontrol && git checkout main && git pull origin main
```

### 2. Создать прод-конфиг из шаблона
```
cp .env.prod.example .env.prod
```

### 3. Вписать реальные значения (те же, что были в Render)
```
nano .env.prod
```
Заполнить: `SECRET_KEY` (ТОТ ЖЕ что в Render!), `OPENAI_API_KEY`,
`TELEGRAM_BOT_TOKEN`, `DATABASE_URL` (Neon), R2-ключи. Сохранить Ctrl+O, выйти Ctrl+X.

### 4. Открыть порты 80 и 443
```
ufw allow 80/tcp && ufw allow 443/tcp
```

### 5. Поставить certbot (если ещё нет)
```
apt-get update && apt-get install -y certbot
```

### 6. Выпустить SSL-сертификат (порт 80 должен быть свободен)
```
certbot certonly --standalone -d trustcontrol.kz -d www.trustcontrol.kz --non-interactive --agree-tos -m ktvc56p8j6@privaterelay.appleid.com
```

### 7. Создать папку для будущих продлений сертификата
```
mkdir -p certbot-www
```

### 8. Поднять бэкенд + nginx
```
docker compose -f docker-compose.prod.yml --env-file .env.prod up -d --build
```

### 9. Проверить что контейнеры живые
```
docker compose -f docker-compose.prod.yml ps
```

### 10. Посмотреть логи api (первый старт прогонит миграции, в т.ч. 0011)
```
docker compose -f docker-compose.prod.yml logs --tail 50 api
```

### 11. Проверить сайт
```
curl -I https://trustcontrol.kz/health
```

---

## Автообновление из main (раз в час)

### Добавить в крон root
```
crontab -e
```
Вписать строку:
```
0 * * * * cd /home/ubuntu/AspanLab/trustcontrol && bash scripts/deploy-backend.sh >> /var/log/tc-deploy.log 2>&1
```
Скрипт сам: подтянет main → пересоберёт если были изменения → продлит SSL → перезагрузит nginx.

---

## Полезное

- Логи api в реальном времени: `docker compose -f docker-compose.prod.yml logs -f api`
- Рестарт только api: `docker compose -f docker-compose.prod.yml restart api`
- Ручной деплой: `bash scripts/deploy-backend.sh`
- Память VPS: `free -h` (следить чтобы swap не был забит постоянно)
- Бэкап БД (Neon делает сам, но можно вручную): `pg_dump "<DATABASE_URL без +asyncpg>" > backup.sql`
